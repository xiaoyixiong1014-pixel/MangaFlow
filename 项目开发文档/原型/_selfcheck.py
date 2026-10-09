# -*- coding: utf-8 -*-
"""原型自检：全站原型的每一页 + 细化原型，逐项检查渲染与 JS 错误。

用法：python _selfcheck.py
"""
import io, os, re, subprocess

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HERE = os.path.dirname(os.path.abspath(__file__))

PRE = ('<script>window.__err=[];window.onerror=function(m,s,l,c){'
       'window.__err.push(m+" @line"+l);return true;};</script>')

# 多页原型：遍历 PAGES
MULTI_POST = ('<script>(function(){var out=[];'
              'try{var ids=Object.keys(P);'
              'ids.forEach(function(id){try{render(id);'
              'var h=document.getElementById("host");'
              'var n=h?h.innerHTML.replace(/\\s/g,"").length:0;'
              'out.push(id+":"+(n>200?"OK("+n+")":"EMPTY("+n+")"));'
              '}catch(e){out.push(id+":THROW("+e.message+")");}});'
              '}catch(e){out.push("FATAL("+e.message+")");}'
              'if(window.__err.length)out.push("ERR["+window.__err.join("; ")+"]");'
              'document.title="RESULT "+out.join("  ");})();</script>')

# 单页原型：只检查错误与主体内容长度
SINGLE_POST = ('<script>(function(){var b=document.body.innerHTML.replace(/\\s/g,"").length;'
               'var e=window.__err.length?("ERR["+window.__err.join("; ")+"]"):"OK";'
               'document.title="RESULT body="+b+" "+e;})();</script>')

MULTI = ["全站原型-一-官网与Studio.html", "全站原型-二-画布与平台页.html", "全站原型-三-运营管理端.html"]
SINGLE = ["W01-工作台-Agent首屏-交互原型.html", "S02-剧本创作-交互原型.html", "S04-资产管理-交互原型.html"]

def run(fn, post):
    p = os.path.join(HERE, fn)
    s = io.open(p, encoding='utf-8').read()
    i = s.find('<script>')
    s = s[:i] + PRE + s[i:]
    j = s.rfind('</body>')
    s = s[:j] + post + s[j:]
    tmp = os.path.join(HERE, '_chk_tmp.html')
    io.open(tmp, 'w', encoding='utf-8').write(s)
    try:
        r = subprocess.run([CHROME, '--headless=new', '--disable-gpu',
                            '--virtual-time-budget=6000', '--dump-dom',
                            'file:///' + tmp.replace('\\', '/')],
                           capture_output=True, timeout=120)
        dom = r.stdout.decode('utf-8', 'ignore')
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    m = re.search(r'<title>([^<]*)</title>', dom)
    return m.group(1) if m else '(无 title)'

total_ok = 0
problems = []

print('=' * 74)
for fn in MULTI:
    title = run(fn, MULTI_POST)
    print(fn)
    if title.startswith('RESULT '):
        for item in title[7:].split('  '):
            print('   ', item)
            if ':OK(' in item or item == 'OK':
                total_ok += 1
            else:
                problems.append(fn + ' -> ' + item)
    else:
        problems.append(fn + ' -> 未取到结果: ' + title[:120])
        print('    未取到结果 ->', title[:120])
    print('-' * 74)

for fn in SINGLE:
    title = run(fn, SINGLE_POST)
    print(fn)
    print('   ', title)
    if title.startswith('RESULT ') and 'ERR[' not in title:
        total_ok += 1
    else:
        problems.append(fn + ' -> ' + title[:120])
    print('-' * 74)

print('通过项：%d' % total_ok)
if problems:
    print('!!! 问题项：')
    for x in problems:
        print('   ', x)
else:
    print('无异常')
print('=' * 74)
