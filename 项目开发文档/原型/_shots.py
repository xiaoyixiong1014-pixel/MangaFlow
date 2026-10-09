# -*- coding: utf-8 -*-
"""换色后视觉抽检截图。"""
import io, os, subprocess

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))

JOBS = [
    ("全站原型-一-官网与Studio.html", 'render("L-01");',  "_v_L01.png"),
    ("全站原型-一-官网与Studio.html", 'render("S-01");',  "_v_S01.png"),
    ("全站原型-一-官网与Studio.html", 'render("S-06");',  "_v_S06.png"),
    ("全站原型-二-画布与平台页.html", 'render("C-02");',  "_v_C02.png"),
    ("全站原型-三-运营管理端.html",   'render("A-01");',  "_v_A04.png"),
]

for src, marker, out in JOBS:
    s = io.open(os.path.join(HERE, src), encoding='utf-8').read()
    # 用对应页替换默认渲染
    for old in ('render("L-01");', 'render("C-01");', 'render("A-01");'):
        if old in s:
            s = s.replace(old, marker, 1)
            break
    tmp = os.path.join(HERE, '_v_tmp.html')
    io.open(tmp, 'w', encoding='utf-8').write(s)
    subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
                    '--screenshot=' + os.path.join(HERE, out),
                    '--window-size=1440,980', '--virtual-time-budget=4000',
                    'file:///' + tmp.replace('\\', '/')],
                   capture_output=True, timeout=90)
    print('shot ->', out)
    os.remove(tmp)
print('done')
