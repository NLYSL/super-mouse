import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from supermouse.core.config import Config
from supermouse.core.bus import EventBus
from supermouse.modes.smart_brain import SmartBrain, skills
from supermouse.modes.smart_brain.plans import resolve, save_custom, custom_path
from supermouse.modes.smart_brain.hover import HoverWatcher

PAGES = 'com.apple.iWork.Pages'


def cfg(tmp_path, **extra):
    return Config({'brain': {'knowledge_dir': str(tmp_path / 'brain'), 'enabled': True, **extra},
                   'eyes': {'gestures': {'hard_blink': {'action': 'none'}}}})


def test_resolution_precedence_and_flat_args(tmp_path):
    c = cfg(tmp_path, apps={PAGES: {'skill':'pages_stock_report', 'report_title':'Configured'},
                           'com.google.Chrome': {'skill':'browser_morning_tabs', 'urls':['https://example.com']}})
    assert resolve(c)[PAGES]['args']['report_title'] == 'Configured'
    assert resolve(c)['com.google.Chrome']['args']['urls'] == ['https://example.com']
    save_custom(c, PAGES, {'title':'Mine', 'skill':'pages_stock_report', 'args':{'report_title':'Mine'}})
    assert resolve(c)[PAGES]['source'] == '自定义任务'
    assert resolve(c)[PAGES]['args'] == {'report_title':'Mine'}
    assert c.get('eyes.gestures.hard_blink.action') == 'none'
    custom_path(c).unlink()
    assert resolve(c)[PAGES]['source'] == '应用配置'


def test_invalid_custom_file_not_overwritten(tmp_path):
    c = cfg(tmp_path)
    custom_path(c).parent.mkdir()
    custom_path(c).write_text('[]')
    assert resolve(c)[PAGES]['skill'] == 'pages_stock_report'
    with pytest.raises(ValueError):
        save_custom(c, PAGES, {'title':'x','skill':'agent'})
    assert custom_path(c).read_text() == '[]'


@pytest.mark.parametrize('skill', ['shell', 'unknown'])
def test_unknown_skill_rejected(tmp_path, skill):
    with pytest.raises(ValueError):
        save_custom(cfg(tmp_path), PAGES, {'title':'x', 'skill':skill})


def test_active_app_dock_is_not_filtered(tmp_path, monkeypatch):
    from supermouse.modes.smart_brain import hover
    b = EventBus(); events=[]; b.subscribe('brain.hover', events.append)
    watcher = HoverWatcher(cfg(tmp_path), b)
    watcher._still_since = 0
    monkeypatch.setattr(hover.macos, 'cursor_pos', lambda:(0,0))
    monkeypatch.setattr(hover.macos, 'frontmost_bundle', lambda:PAGES)
    monkeypatch.setattr(hover.macos, 'element_under_cursor', lambda *a:dict(kind='dock', app=PAGES))
    watcher._poll()
    assert events[0]['app'] == PAGES
    watcher._fired_at = None; watcher._recent.clear()
    monkeypatch.setattr(hover.macos, 'element_under_cursor', lambda *a:dict(kind='window', app=PAGES))
    watcher._poll()
    assert len(events) == 1


@pytest.mark.parametrize('status', ['done','partial','error'])
def test_runtime_preserves_skill_status(tmp_path, monkeypatch, status):
    async def run():
        b=EventBus(); events=[]; b.subscribe('*', events.append)
        brain=SmartBrain(cfg(tmp_path),b)
        monkeypatch.setitem(skills.REGISTRY,'test',lambda ctx: skills.SkillResult(status,'result','/tmp/a.docx',status!='error'))
        await brain._execute({'plan_id':'test', 'skill':'test'})
        action=[e for e in events if e['type']=='brain.action'][-1]
        assert action['status'] == status
        assert action['artifact_path'] == '/tmp/a.docx'
        assert events[-1]['anim'] == ('happy' if status=='done' else 'idle')
    asyncio.run(run())


def test_unknown_named_skill_never_runs_agent(tmp_path):
    async def run():
        b=EventBus(); events=[]; b.subscribe('brain.action',events.append)
        brain=SmartBrain(cfg(tmp_path),b)
        await brain._execute({'plan_id':'t','skill':'bogus'})
        assert events[-1]['status']=='error'
    asyncio.run(run())


def test_confirm_policy_and_manual_test(tmp_path, monkeypatch):
    async def run():
        b=EventBus(); c=cfg(tmp_path); brain=SmartBrain(c,b); calls=[]
        async def execute(p): calls.append(p)
        monkeypatch.setattr(brain,'_execute',execute)
        brain._offer({'title':'report', 'skill':'pages_stock_report','confirmation':'click'}, {'app':PAGES})
        brain._on_confirm({'type':'ui.confirm_gesture'})
        assert not calls and brain.pending
        brain._on_confirm({'type':'ui.click_suggest','plan_id':'wrong'})
        assert brain.pending
        brain._on_confirm({'type':'ui.click_suggest'})
        await brain.running
        assert len(calls)==1
        c.set('brain.enabled',False)
        brain._test({'app':PAGES})
        assert len(calls)==1
        c.set('brain.enabled',True)
        brain._test({'app':PAGES})
        await brain.running
        assert calls[-1]['skill']=='pages_stock_report'
    asyncio.run(run())


def test_cancel_keeps_worker_busy(tmp_path, monkeypatch):
    async def run():
        gate=threading.Event(); started=threading.Event()
        def worker(ctx):
            started.set(); gate.wait(2); return 'old'
        monkeypatch.setitem(skills.REGISTRY,'test',worker)
        brain=SmartBrain(cfg(tmp_path),EventBus())
        brain.running=asyncio.create_task(brain._execute({'plan_id':'old','skill':'test'}))
        while not started.is_set(): await asyncio.sleep(.01)
        brain._on_stop({})
        try: await brain.running
        except asyncio.CancelledError: pass
        assert brain._busy()
        assert brain.ctx.cancelled
        gate.set(); await brain._worker
        assert not brain._busy()
    asyncio.run(run())


@pytest.mark.parametrize('quotes,err,opened,status', [
    ([dict(code='s_sh000001',name='上证',price=1,change=0,pct=0)],None,True,'done'),
    ([], 'offline',True,'partial'),
    ([], 'offline',False,'error'),
    ([dict(code='s_sh000001',name='上证',price=1,change=0,pct=0)],'missing',True,'partial'),
])
def test_report_results(tmp_path, monkeypatch, quotes, err, opened, status):
    monkeypatch.setattr(skills,'_fetch_quotes', lambda _: (quotes,err))
    def convert(args, **kwargs):
        Path(args[args.index('-output')+1]).write_bytes(b'test-docx')
        return SimpleNamespace(returncode=0, stderr='')
    monkeypatch.setattr(skills.subprocess,'run',convert)
    monkeypatch.setattr(skills,'_open_pages',lambda p:(opened,'ERROR: denied' if not opened else 'doc'))
    result=skills.pages_stock_report(skills.SkillContext(EventBus(),'t',cfg(tmp_path)), output_dir=str(tmp_path),report_title='我的报告',notes='观察备注')
    assert result.status==status
    assert result.opened==opened
    assert Path(result.artifact_path).exists()
    if quotes:
        assert any('上证' in text for _,text in skills._build_report(quotes,err))


def test_conversion_failure_not_success(tmp_path, monkeypatch):
    monkeypatch.setattr(skills,'_fetch_quotes',lambda _:([],'offline'))
    monkeypatch.setattr(skills.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1,stderr='conversion failed'))
    opener=Mock(); monkeypatch.setattr(skills,'_open_pages',opener)
    result=skills.pages_stock_report(skills.SkillContext(EventBus(),'t',cfg(tmp_path)),output_dir=str(tmp_path))
    assert result.status=='error'
    opener.assert_not_called()


def test_open_verification_requires_document(monkeypatch):
    for value, expected in [('',False),('ERROR: denied',False),('Report',True)]:
        monkeypatch.setattr(skills.macos,'osa', lambda *a, **kw:value)
        assert skills._open_pages(Path('/tmp/a.docx'))[0] is expected


def test_rtf_non_bmp():
    assert skills._rtf_escape('😀') == r'\u-10179?\u-8704?'


def test_quote_parser_rejects_invalid_and_reports_missing(monkeypatch):
    import urllib.request
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self):
            return b'var hq_str_s_sh000001="index,10,1,2";\nvar hq_str_s_sz399001="bad,nan,1,2";'
    monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**kw:Response())
    quotes,err=skills._fetch_quotes(skills._DEFAULT_SYMBOLS)
    assert len(quotes)==1
    assert 's_sz399001' in err


def test_bad_config_args_does_not_break_builtin(tmp_path):
    c=cfg(tmp_path,apps={PAGES:{'args':[]}})
    assert resolve(c)[PAGES]['source']=='内置预设'


def test_new_hover_cancels_stale_ai_suggestion(tmp_path, monkeypatch):
    async def run():
        brain=SmartBrain(cfg(tmp_path),EventBus())
        async def slow(): await asyncio.sleep(10)
        brain._suggest_task=asyncio.create_task(slow())
        await asyncio.sleep(0)
        brain._on_hover({'app':PAGES,'kind':'dock'})
        await asyncio.sleep(0)
        assert brain._suggest_task.cancelled()
        assert brain.pending['skill']=='pages_stock_report'
        brain._expire('stale-id')
        assert brain.pending
        brain._on_stop({})
    asyncio.run(run())


def test_settings_ui_save_and_reload_preserves_bindings(tmp_path):
    import os
    import subprocess
    import sys
    script = '''
import sys
from PyQt6.QtWidgets import QApplication
from supermouse.core.config import Config
from supermouse.core.bus import EventBus
from supermouse.ui.brain_settings import BrainSettings
app=QApplication([])
c=Config({'brain':{'knowledge_dir':sys.argv[1]}, 'eyes':{'gestures':{'hard_blink':{'action':'none'}}}})
b=EventBus();events=[];b.subscribe('*',events.append)
d=BrainSettings(c,b)
d.report_title.setText('我的股市报告')
assert d.save()
d.load()
assert d.report_title.text()=='我的股市报告'
assert c.get('eyes.gestures.hard_blink.action')=='none'
assert not any(e['type']=='brain.test' for e in events)
d.test()
assert events[-1]['type']=='brain.test'
d.app.setCurrentText('com.example.Custom')
d.title.setText('自定义任务')
d.skill.setCurrentText('agent')
d.instruction.setPlainText('打开应用并创建草稿，不发送')
assert d.save()
d.load()
assert d.instruction.toPlainText()=='打开应用并创建草稿，不发送'
'''
    subprocess.run([sys.executable,'-c',script,str(tmp_path/'ui')],
                   env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},check=True,timeout=20)
