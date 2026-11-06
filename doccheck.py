import sys, types, collections
pyte=types.ModuleType('pyte'); screens=types.ModuleType('pyte.screens')
screens.Margins=collections.namedtuple('Margins','top bottom')
class _C:
    def __init__(s,d=' '): s.data=d
class _Cur:
    def __init__(s): s.x=0; s.y=0
class Screen(object):
    def __init__(s,columns,lines):
        s.columns=columns; s.lines=lines; s.cursor=_Cur(); s.margins=None; s.dirty=set()
        s.buffer=collections.defaultdict(lambda: collections.defaultdict(_C))
    def index(s): pass
    def draw(s,t): pass
    def bell(s): pass
pyte.Screen=Screen; pyte.screens=screens; pyte.Stream=type('S',(),{'csi':{}})
sys.modules['pyte']=pyte; sys.modules['pyte.screens']=screens
wc=types.ModuleType('wcwidth'); wc.wcwidth=lambda c:1
sys.modules['wcwidth']=wc
import os
REPO=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0,REPO)
import tdsr.tdsr as T
class FS(Screen):
    def __init__(s,c,l,rows):
        Screen.__init__(s,c,l)
        for y,t in enumerate(rows):
            for x in range(c): s.buffer[y][x]=_C(t[x] if x<len(t) else ' ')
spoken=[]
T.synth=types.SimpleNamespace(send=lambda s: spoken.append(s))
T.state.ignore_re=None; T.state.punctuation_re=None
T.state.history=collections.deque(maxlen=5)


import re
readme=open(os.path.join(REPO,'readme.md')).read()
keys_sec=readme.split('## Keys')[1].split('## Configuration')[0]
cfg_sec=readme.split('## Configuration')[1].split('## Selecting')[0]

def spec_to_prose(spec):
    parts=spec.split()
    out=[]
    for p in parts:
        p=p.replace('alt+shift+','alt shift ').replace('alt+','alt ')
        p=p.replace(',','comma').replace('.','dot')
        out.append(p)
    return out[0]

print("== commands missing from the readme Keys section ==")
missing=[]
for name,spec in sorted(T.DEFAULT_KEYS.items()):
    prose=spec_to_prose(spec)
    # crude: look for the key prose anywhere in the Keys section
    base=prose.replace('alt shift ','alt shift ').strip()
    if base not in keys_sec:
        missing.append((name,spec,base))
for n,s,b in missing: print("   %-22s %-16s (looked for %r)"%(n,s,b))
if not missing: print("   none")

print()
print("== config menu letters in code vs readme ==")
import inspect
code=inspect.getsource(T.ConfigHandler.__init__)
letters=re.findall(r"b'(.)': self\.", code)
for L in letters:
    forms = [L]
    if L.isupper():
        forms.append('shift ' + L.lower())
    if any(re.search(r'^\* %s - ' % re.escape(f), cfg_sec, re.M) for f in forms):
        continue
    print("   MISSING from readme: %r"%L)
print("   (checked: %s)"%", ".join(letters))

print()
print("== copy mode letters ==")
ccode=inspect.getsource(T.CopyHandler.__init__)
cl=re.findall(r"b'(.)': self\.", ccode)
print("   code:",cl)
print("   readme mentions l/s/a:", all(("`%s`"%x) in readme for x in cl))

print()
print("== config settings in code vs readme ==")
defaults=dict(T.state.config['speech'])
for k in sorted(defaults):
    if k not in readme:
        print("   setting not documented: %s"%k)
print("   (checked %d settings)"%len(defaults))

print()
print("== ai settings in code vs readme ==")
for k in sorted(T.state.config['ai']):
    if k not in readme:
        print("   ai setting not documented: %s"%k)
print("   (checked %d ai settings)"%len(T.state.config['ai']))

print()
print("== stale claims ==")
km=T.build_keymap()
if 'Press twice for the oldest' in readme:
    print("   STALE: alt shift u twice claim, bound=%s"%(b'\x1bU\x1bU' in km))
else:
    print("   alt shift u twice claim removed: ok")
