# -*- coding: utf-8 -*-
"""手机号 / 身份证 / 银行卡 查询（群里或私聊直接发号码就行）

★ 数据来源（2026-10-08 定的，改之前先看一眼为什么）：
  · 手机号  → **离线库** `assets/phone.dat`（手机号段归属地，49.9 万条）
  · 身份证  → **离线表** `assets/idcode.txt`（民政部区划代码，3351 条）
  · 银行卡  → 支付宝的公开校验接口（**联网**，给银行名和卡类型）

★★ 手机号为什么用离线库、不联网查：
  那天把能找到的免费接口全试了一遍（vvhan / oioweb / 52vmy / taobao /
  uomg / tenapi / kuleu / peark / aa1 …），**一个能用的都没有** ——
  要么证书过期、要么 502、要么直接返反爬页面。离线库又快又稳，
  还不用看别人脸色（手机号段一年也就更新一两次）。
  ★ 库文件是从 ls0f/phone 仓库取的，格式见下面 _load_phone_db()。

★ 银行卡**查不到开户行所在地** —— 支付宝这个接口只给银行和卡类型。
  免费又稳定的「卡号 → 开户行省份城市」来源确实没有，别硬编一个。
"""
from __future__ import annotations

import os
import re
import struct
import threading

import httpx

# ------------------------------------------------------------------ 数据文件
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
PHONE_DAT = os.path.join(_ASSETS, 'phone.dat')
IDCODE_TXT = os.path.join(_ASSETS, 'idcode.txt')

# ★ 不用 core.resource_path：打包后这两个文件在 `runners/ledger/assets/` 下，
#   而本模块的 __file__ 正好也在那层 —— 冻不冻在 exe 里都能算对。
#   （前提：spec 的 datas 里要带上它们，少了就报「数据文件没找到」）

_phone_lock = threading.Lock()
_phone_buf: bytes | None = None
_phone_count = 0
_phone_first = 0

_id_lock = threading.Lock()
_id_rows: dict | None = None

# 手机号段库里的运营商代码 → 中文（格式是人家定死的，别改数值）
CARRIERS = {1: "移动", 2: "联通", 3: "电信", 4: "电信虚拟运营商",
            5: "联通虚拟运营商", 6: "移动虚拟运营商", 7: "广电",
            8: "广电虚拟运营商"}

# 银行卡 BIN 接口返回的是银行英文代号，这里换成中文。
# ★ 认不出来就**原样显示代号**，不猜（猜错比不显示还糟）
BANKS = {
    "ICBC": "中国工商银行", "ABC": "中国农业银行", "CCB": "中国建设银行",
    "BOC": "中国银行", "BCM": "交通银行", "PSBC": "中国邮政储蓄银行",
    "CMB": "招商银行", "SPDB": "浦发银行", "CITIC": "中信银行",
    "CEB": "中国光大银行", "HXB": "华夏银行", "CMBC": "中国民生银行",
    "GDB": "广发银行", "CIB": "兴业银行", "PAB": "平安银行",
    "BOB": "北京银行", "BOS": "上海银行", "NBCB": "宁波银行",
    "HZBANK": "杭州银行", "NJCB": "南京银行", "CZB": "浙商银行",
    "HSBANK": "徽商银行", "JSBANK": "江苏银行", "CBHB": "渤海银行",
    "BEA": "东亚银行", "HSBC": "汇丰银行", "SCB": "渣打银行",
    "CGB": "广发银行", "SRCB": "上海农商银行", "BJRCB": "北京农商银行",
    "GZRCB": "广州农商银行", "ZJTLCB": "浙江泰隆商业银行",
    "HKBEA": "东亚银行", "EGBANK": "恒丰银行", "SHRCB": "上海农商银行",
    "LYB": "洛阳银行", "ZZB": "郑州银行", "HKB": "汉口银行",
    "CSRCB": "长沙银行", "SXRCU": "山西省农村信用社",
    "COMM": "交通银行", "SPABANK": "平安银行",
}

CARD_TYPES = {"DC": "借记卡（储蓄卡）", "CC": "信用卡",
              "SCC": "准贷记卡", "PC": "预付费卡"}

BANK_API = ("https://ccdcapi.alipay.com/validateAndCacheCardInfo.json"
            "?_input_charset=utf-8&cardNo=%s&cardBinCheck=true")
BANK_TIMEOUT = 8.0

# ★★ 百度 API 商城的「银行卡基本信息」—— 查得到**归属地 / 联行号 / 银行电话**
#    那一整套（就是用户 2026-10-08 拿别人机器人截图来问的那个）。
#    调用方式是从商品页的示例代码里抄的，别凭印象改：
#      GET  .../lundear/qryCardInfo?cardno=<卡号>
#      请求头  X-Bce-Signature: AppCode/<你的AppCode>
#    返回 data：
#      bankCode(总行联行号) bankId(银行编码) bankName abbr(英文缩写)
#      cardName(卡名称) cardType(卡类型) cardBin binLen
#      area(卡所在地区) bankPhone bankUrl bankLogo
#      code=0 成功、code=2 无效卡号
#    ★ 官方示例写的是 http:// —— 我们走 **https**（实测支持）。
#      卡号不该在公网上走明文。
#    ★★ 它是**要花钱的**（0元/50次试用，之后约 46 元/万次），
#      所以只有主人配了 AppCode 才走这条路；没配就退回支付宝那个免费接口。
RICH_API = "https://qrycardinfo.api.bdymkt.com/lundear/qryCardInfo?cardno=%s"


class LookupError(Exception):
    """查不了（数据文件缺失之类）。**别把异常吞掉换成编的答案**"""


# ------------------------------------------------------------------ 识别
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_ID_RE = re.compile(r"\d{17}[0-9Xx]")
_BANK_RE = re.compile(r"\d{16,19}")

# 身份证校验位（GB 11643-1999，ISO 7064:1983 MOD 11-2）
_ID_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CK = "10X98765432"


def looks_like_id(num: str) -> bool:
    """18 位的一串，看着像不像身份证（不看校验位）

    判据两条都要满足：前 6 位是**真实存在的区划码**、第 7~14 位是
    **一个像样的生日**。银行卡前 6 位是 BIN，几乎不可能同时撞上这两条。
    """
    if len(num) != 18 or not num[:17].isdigit():
        return False
    if not _ID_RE.fullmatch(num):
        return False
    try:
        region_ok = bool(idcard_region(num))
    except LookupError:
        region_ok = False
    if not region_ok:
        return False
    try:
        y, m, d = int(num[6:10]), int(num[10:12]), int(num[12:14])
    except ValueError:
        return False
    return 1940 <= y <= 2035 and 1 <= m <= 12 and 1 <= d <= 31


def detect(text: str):
    """这段文字是不是一个「能查的号码」。返回 (类型, 号码) 或 None

    ★ 只认**整条消息就是一个号码**（前后可以有空格）——
      不然群里随便一句话里带个数字就会被当成查询，太吵了。
    ★ 18 位数字**既是身份证也是银行卡**，靠两点分：
      ① 校验位对 → 身份证
      ② 校验位不对、但看着就是身份证（区划码真实 + 生日像样）
         → 还是身份证（好告诉他「校验位不对，这号是编的」，
           而不是含糊地回一句「查不到这个卡号」）
      ③ 都不像 → 当银行卡
    """
    t = (text or "").strip()
    if not t:
        return None
    # ★ 银行卡号常按 4 位一组敲空格（6222 0202 …）—— 只有「整条都是
    #   数字和空格」才允许去掉空格，带字的句子一律不算号码
    if " " in t or "　" in t:
        compact = t.replace(" ", "").replace("　", "")
        if compact.isdigit() and _BANK_RE.fullmatch(compact):
            return "bankcard", compact
        return None
    if _PHONE_RE.fullmatch(t):
        return "phone", t
    if _ID_RE.fullmatch(t) and (id_checksum_ok(t) or looks_like_id(t)):
        return "idcard", t
    if _BANK_RE.fullmatch(t):
        return "bankcard", t
    return None


def id_checksum_ok(num: str) -> bool:
    """身份证校验位对不对（18 位）"""
    if not num or len(num) != 18:
        return False
    body = num[:17]
    if not body.isdigit():
        return False
    total = sum(int(body[i]) * _ID_W[i] for i in range(17))
    return _ID_CK[total % 11] == num[17].upper()


# ------------------------------------------------------------------ 手机号
def _load_phone_db():
    """把 phone.dat 读进内存（只读一次）。

    文件格式（ls0f/phone 定的）：
      头 8 字节  = 4 字节版本 + 4 字节「索引区起始偏移」
      索引区     = 每条 9 字节：`<iiB` 前7位前缀 / 记录偏移 / 运营商代码
      记录区     = `省|市|邮编|区号`，用 \\x00 结尾
    ★ 索引按前缀**升序**排的，所以能二分（49.9 万条，线性扫也行但没必要）
    """
    global _phone_buf, _phone_count, _phone_first
    if _phone_buf is not None:
        return
    with _phone_lock:
        if _phone_buf is not None:
            return
        if not os.path.exists(PHONE_DAT):
            raise LookupError(
                "手机号库没找到（%s）—— 服务器上重新部署一次，"
                "打包 exe 的话要把它加进 spec 的 datas" % PHONE_DAT)
        with open(PHONE_DAT, "rb") as f:
            buf = f.read()
        rec = struct.calcsize("<iiB")
        _phone_first = struct.unpack("<4si", buf[:8])[1]
        _phone_count = (len(buf) - _phone_first) // rec
        _phone_buf = buf


def phone_info(num: str) -> dict:
    """手机号 → {province, city, carrier, area_code, zip_code}"""
    _load_phone_db()
    key = int(num[:7])
    lo, hi = 0, _phone_count - 1
    rec = struct.calcsize("<iiB")
    buf = _phone_buf
    while lo <= hi:
        mid = (lo + hi) // 2
        off = _phone_first + mid * rec
        pref, roff, ptype = struct.unpack("<iiB", buf[off:off + rec])
        if key < pref:
            hi = mid - 1
        elif key > pref:
            lo = mid + 1
        else:
            end = buf.find(b"\x00", roff)
            parts = buf[roff:end].decode("utf-8", "replace").split("|")
            while len(parts) < 4:
                parts.append("")
            return {"province": parts[0], "city": parts[1],
                    "zip_code": parts[2], "area_code": parts[3],
                    "carrier": CARRIERS.get(ptype, "未知")}
    return {}


def _region_text(province: str, city: str) -> str:
    """「北京 北京」这种重复的只留一个（库里就是这么存的）"""
    province = (province or "").strip()
    city = (city or "").strip()
    if not city or city == province:
        return province
    return "%s %s" % (province, city)


# ------------------------------------------------------------------ 身份证
def _load_id_table():
    global _id_rows
    if _id_rows is not None:
        return
    with _id_lock:
        if _id_rows is not None:
            return
        if not os.path.exists(IDCODE_TXT):
            raise LookupError(
                "身份证区划表没找到（%s）—— 用 _build_idcode.py 生成，"
                "或者重新部署一次" % IDCODE_TXT)
        rows = {}
        with open(IDCODE_TXT, encoding="utf-8") as f:
            for line in f:
                code, _, name = line.rstrip("\n").partition("\t")
                if code and name:
                    rows[code] = name
        _id_rows = rows


def idcard_region(num: str) -> str:
    """身份证前 6 位 → 归属地。查不到返回空串

    ★ 六位查不到时依次退到**前 4 位（市）**、**前 2 位（省）** ——
      有些老身份证是撤销掉的区划码，退一级总比什么都没查出来强。
    """
    _load_id_table()
    for n in (6, 4, 2):
        got = _id_rows.get(num[:n])
        if got:
            return got
    return ""


# ------------------------------------------------------------------ 银行卡
def _bank_info_rich(card: str, appcode: str) -> dict:
    """走百度那个**付费**接口 —— 有归属地、联行号、银行电话

    ★ code=0 才算成功；**code=2 是「无效卡号」**（不是出错，别当成故障）。
    """
    with httpx.Client(timeout=BANK_TIMEOUT, trust_env=False,
                      headers={"User-Agent": "Mozilla/5.0",
                               "X-Bce-Signature": "AppCode/%s" % appcode,
                               "Content-Type": "application/json;charset=UTF-8"}
                      ) as client:
        r = client.get(RICH_API % card)
        r.raise_for_status()
        payload = r.json()
    if str(payload.get("code")) != "0":
        return {}
    d = payload.get("data") or {}
    if not d:
        return {}
    return {
        "source": "rich",
        "bank_name": (d.get("bankName") or "未知").strip(),
        "abbr": (d.get("abbr") or "").strip(),
        "card_name": (d.get("cardName") or "").strip(),
        "card_type": (d.get("cardType") or "").strip(),
        "card_bin": (d.get("cardBin") or "").strip(),
        "area": (d.get("area") or "").strip(),
        "bank_code": (d.get("bankCode") or "").strip(),
        "bank_id": (d.get("bankId") or "").strip(),
        "bank_phone": (d.get("bankPhone") or "").strip(),
    }


def bank_info(card: str, appcode: str = "") -> dict:
    """银行卡号 → 银行/卡类型（+ 配了密钥时的归属地、联行号…）

    ★ **联网**。查不到就返回空 dict，别瞎猜一个银行出来
      （猜错了会直接害到人）。
    ★ 没配 AppCode 时走支付宝那个免费接口 —— 只有银行名和卡类型
      （它**不给开户地**，免费又稳定的来源确实没有）。
    ★★ 配了 AppCode 但**调用失败**（网络 / 密钥过期 / 额度用完）时：
      **退回免费接口**，并在结果里留一条 note 说明降级了 ——
      不能因为付费那条挂了就整条查询报错，更不能悄悄少给东西不说。
    """
    note = ""
    if appcode:
        try:
            got = _bank_info_rich(card, appcode)
            if got:
                return got
            return {}          # 接口说「无效卡号」—— 那就是真查不到
        except Exception as e:
            note = "归属地暂时查不到（%s），下面是免费接口的结果" % \
                   type(e).__name__
    with httpx.Client(timeout=BANK_TIMEOUT, trust_env=False,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        r = client.get(BANK_API % card)
        r.raise_for_status()
        data = r.json()
    if not data.get("validated"):
        return {}
    code = (data.get("bank") or "").strip()
    return {"source": "basic",
            "bank": code,
            "bank_name": BANKS.get(code.upper(), code or "未知"),
            "card_type": CARD_TYPES.get((data.get("cardType") or "").upper(),
                                        data.get("cardType") or "未知"),
            "note": note}


# ------------------------------------------------------------------ 出卡片
def format_card(kind: str, num: str, info: dict) -> str:
    """拼成群里那条回复（HTML）

    ★ 号码**原样显示**：是他自己在群里发出来查的，遮一半反而对不上账
      （群里本来也都看见那个号了）。
    ★ 查不到就说查不到，**绝不编**。
    """
    head = {"phone": "📱 手机号归属地", "idcard": "🪪 身份证归属地",
            "bankcard": "💳 银行卡信息"}[kind]
    lines = ["<b>%s</b>" % head, "━━━━━━━━━━━━━━", "号码：<code>%s</code>" % num]
    if kind == "phone":
        if not info:
            lines.append("这个号段没查到（可能是很新的号段）")
        else:
            lines.append("归属地：%s" % _region_text(info.get("province", ""),
                                                info.get("city", "")))
            lines.append("运营商：%s" % info.get("carrier", "未知"))
            if info.get("area_code"):
                lines.append("区号：%s" % info["area_code"])
    elif kind == "idcard":
        region = info.get("region") or ""
        lines.append("归属地：%s" % (region or "没查到（可能是撤掉的区划码）"))
        lines.append("校验：%s" % ("✅ 号码格式正确" if info.get("valid")
                                  else "❌ 校验位不对，这个号是编的"))
    else:
        if not info:
            lines.append("没查到这个卡号 —— 可能卡号不对，"
                         "或者这家银行不在支持范围里")
        elif info.get("source") == "rich":
            bank = info.get("bank_name", "未知")
            if info.get("abbr"):
                bank += "（%s）" % info["abbr"]
            lines.append("银行：%s" % bank)
            if info.get("card_name"):
                lines.append("卡名称：%s" % info["card_name"])
            lines.append("类型：%s" % (info.get("card_type") or "未知"))
            if info.get("card_bin"):
                lines.append("卡bin：%s" % info["card_bin"])
            if info.get("area"):
                lines.append("归属地：%s" % info["area"])
            if info.get("bank_code"):
                lines.append("总行联行号：%s" % info["bank_code"])
            if info.get("bank_phone"):
                lines.append("银行电话：%s" % info["bank_phone"])
        else:
            lines.append("银行：%s" % info.get("bank_name", "未知"))
            lines.append("类型：%s" % info.get("card_type", "未知"))
            if info.get("note"):
                # ★ 只有「配了密钥但没查成」才出这句（是异常，得说）
                lines.append("⚠️ %s" % info["note"])
    return "\n".join(lines)
