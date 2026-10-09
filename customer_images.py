"""Authenticated welcome images, kept locally without exposing Telegram tokens."""
import base64
import io
import hashlib
import os
import re
from pathlib import Path

import requests
from PIL import Image, ImageOps

import core
import customer_config as CC

MAX_IMAGE = 6 * 1024 * 1024
LOCAL_PHOTO = 'local-welcome'


def decode_image(image):
    if not isinstance(image, str) or len(image) > MAX_IMAGE * 4 // 3 + 100:
        raise ValueError('请选择不超过6MB的图片')
    match = re.fullmatch(r'data:image/(?:jpeg|png|webp);base64,([A-Za-z0-9+/=]+)', image)
    if not match:
        raise ValueError('图片格式无效')
    try:
        raw = base64.b64decode(match[1], validate=True)
    except ValueError:
        raise ValueError('图片格式无效') from None
    return normalize(raw)


def ad_path(bot, image):
    if not isinstance(image, str) or not re.fullmatch(r'[a-f0-9]{64}', image):
        raise ValueError('广告图片标识无效')
    return image_path(bot).with_name(bot['id'] + '.ad-' + image + '.jpg')


def ad_action(mgr, bot, uid, payload):
    grants = CC.grants(bot, uid)
    if not (grants['manage'] or grants['broadcast']):
        raise PermissionError('没有广告图片管理权限')
    if payload.get('mode') == 'upload':
        raw = decode_image(payload.get('image'))
        ref = hashlib.sha256(raw).hexdigest()
        path = ad_path(bot, ref)
        with mgr.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temp = path.with_suffix('.jpg.tmp')
                temp.write_bytes(raw)
                os.replace(temp, path)
        return dict(ref=ref)
    if payload.get('mode') == 'preview':
        try:
            raw = ad_path(bot, payload.get('ref')).read_bytes()
            return dict(image='data:image/jpeg;base64,' + base64.b64encode(raw).decode())
        except OSError:
            raise ValueError('广告图片暂时无法读取，请重新选择') from None
    raise ValueError('图片操作无效')


def image_path(bot):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', bot['id']):
        raise ValueError('机器人标识无效')
    return Path(core.bot_data_dir(bot)) / (bot['id'] + '.welcome.jpg')


def normalize(raw):
    if not raw or len(raw) > MAX_IMAGE:
        raise ValueError('请选择不超过6MB的图片')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP') or image.width * image.height > 20000000 or max(image.size) > min(image.size) * 20:
                raise ValueError('invalid image')
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1280, 1280))
            rgba = image.convert('RGBA')
            output = Image.new('RGB', rgba.size, 'white')
            output.paste(rgba, mask=rgba.getchannel('A'))
            result = io.BytesIO()
            output.save(result, format='JPEG', quality=85)
            return result.getvalue()
    except Exception:
        raise ValueError('图片无法读取，请选择正常的JPG、PNG或WebP图片') from None


def preview(bot):
    photo = (bot.get('ledger') or {}).get('newbie_welcome_photo')
    if not photo:
        return dict(image=None)
    try:
        if photo == LOCAL_PHOTO:
            raw = image_path(bot).read_bytes()
        else:
            file = core.TgAPI(bot['token']).call('getFile', file_id=photo)
            path = file.get('file_path', '')
            if not re.fullmatch(r'[\w./-]+', path) or '..' in path.split('/') or path.startswith('/') or file.get('file_size', 0) > MAX_IMAGE:
                raise ValueError('invalid file')
            with requests.get('https://api.telegram.org/file/bot' + bot['token'] + '/' + path,
                              stream=True, allow_redirects=False, timeout=(5, 15)) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_IMAGE:
                        raise ValueError('image too large')
                    chunks.append(chunk)
                raw = b''.join(chunks)
        return dict(image='data:image/jpeg;base64,' + base64.b64encode(normalize(raw)).decode())
    except Exception:
        raise ValueError('当前欢迎图片暂时无法读取，可以重试、替换或移除') from None


def action(mgr, bot, uid, payload):
    if not CC.grants(bot, uid)['manage']:
        raise PermissionError('没有欢迎图片管理权限')
    mode = payload.get('mode')
    if mode == 'preview':
        return preview(bot)
    raw = None
    if mode == 'upload':
        raw = decode_image(payload.get('image'))
    elif mode != 'remove':
        raise ValueError('图片操作无效')
    with mgr.lock:
        cfg = CC.settings(bot)
        if type(payload.get('revision')) is not int or payload['revision'] != cfg['revision']:
            raise ValueError('配置已被其他人修改，请刷新后重试')
        ledger = bot.setdefault('ledger', {})
        if raw is not None:
            path = image_path(bot)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.jpg.tmp')
            temp.write_bytes(raw)
            os.replace(temp, path)
            ledger['newbie_welcome_photo'] = LOCAL_PHOTO
        else:
            ledger.pop('newbie_welcome_photo', None)
        cfg['revision'] += 1
        ledger['customer'] = cfg
        mgr.save()
    return {}
