"""Remove only expired generated artifacts; never touch bills or SQLite journals."""
import os
from pathlib import Path
import re
import time


def cleanup(manager, bot, root):
    root = Path(root).resolve()
    data = root/'data'
    if data.is_symlink():
        return
    with manager.lock:
        ledger = bot.get('ledger') or {}
        references = {r.get('image') for r in (ledger.get('customer') or {}).get('ads',[]) if isinstance(r,dict)}
        keep_welcome = ledger.get('newbie_welcome_photo') == 'local-welcome'
        # Only generated files with the exact current bot ID can be deleted.
        prefix = re.escape(bot['id'])
        generated = re.compile(prefix+r'\.ad-([a-f0-9]{64})\.jpg$')
        for path in data.iterdir() if data.exists() else ():
            try:
                if path.is_symlink() or not path.is_file() or time.time()-path.stat().st_mtime<48*3600:
                    continue
                ad = generated.fullmatch(path.name)
                unused = bool(ad and ad[1] not in references)
                unused |= path.name == bot['id']+'.welcome.jpg' and not keep_welcome
                unused |= bool(re.fullmatch(prefix+r'\.(?:welcome|ad-[a-f0-9]{64})\.jpg\.tmp',path.name))
                unused |= bool(re.fullmatch(prefix+r'\.json\.[a-zA-Z0-9_-]+\.tmp',path.name))
                if unused:
                    path.unlink()
            except OSError:
                pass
        for path in root.iterdir():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if re.fullmatch(r'(?:config|bots|runtime)\.json\.[a-zA-Z0-9_-]+\.tmp',path.name) and time.time()-path.stat().st_mtime>48*3600:
                    path.unlink()
            except OSError:
                pass
    # Keep the same inode because the supervisor has process.log open with O_APPEND.
    for name in ('process.log','运行日志.txt'):
        path = root/name
        try:
            if path.is_symlink() or path.stat().st_size<=2*1024*1024:
                continue
            with path.open('rb+') as stream:
                stream.seek(-1024*1024,os.SEEK_END)
                stream.readline()
                tail = stream.read()
                stream.seek(0)
                stream.write(tail)
                stream.truncate()
        except OSError:
            pass
