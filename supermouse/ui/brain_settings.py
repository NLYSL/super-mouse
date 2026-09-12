"""User-owned Brain recipes; save and test use the same runtime resolver."""
import json
from pathlib import Path
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QComboBox,
    QLineEdit, QPlainTextEdit, QPushButton, QLabel, QHBoxLayout)
from ..modes.smart_brain.plans import resolve, save_custom
from ..modes.smart_brain.skills import REGISTRY, _DEFAULT_SYMBOLS
from ..actions import macos


class BrainSettings(QDialog):
    event_received = pyqtSignal(dict)

    def __init__(self, cfg, bus, parent=None):
        super().__init__(parent)
        self.cfg, self.bus = cfg, bus
        self.artifact = ''
        self.setWindowTitle('Brain · 自定义任务')
        self.resize(620, 680)
        self.setStyleSheet('QDialog {background:#202020;} QPushButton {background:#363636; border:1px solid #555; border-radius:4px; padding:6px;} QPushButton:hover {background:#454545;} QLabel,QLineEdit,QPlainTextEdit,QComboBox,QPushButton {color:#eeeeee;} QLineEdit,QPlainTextEdit,QComboBox {background:#303030;}')
        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)
        self.app = QComboBox()
        self.app.setEditable(True)
        self.app.addItems(sorted(resolve(cfg)))
        form.addRow('目标应用 Bundle ID', self.app)
        self.source = QLabel()
        self.source.setMaximumHeight(28)
        form.addRow('当前来源', self.source)
        self.title = QLineEdit()
        form.addRow('任务名称', self.title)
        self.skill = QComboBox()
        self.skill.addItems([*REGISTRY, 'agent'])
        form.addRow('执行技能（agent 为自定义 AI）', self.skill)
        self.instruction = QPlainTextEdit()
        self.instruction.setMaximumHeight(70)
        self.instruction.setPlaceholderText('仅 agent 使用：描述要完成的任务；需配置在线模型。')
        form.addRow('AI 任务要求', self.instruction)
        self.confirmation = QComboBox()
        self.confirmation.addItem('点击 / 空格 / 已绑定的 Brain 眼势', 'any')
        self.confirmation.addItem('仅点击 / 空格，不接受眼势', 'click')
        form.addRow('悬停后的确认', self.confirmation)
        self.report_title = QLineEdit()
        self.symbols = QLineEdit()
        self.symbols.setPlaceholderText('s_sh000001,s_sz399001,s_sh000300,s_sz399006')
        self.output = QLineEdit()
        self.notes = QLineEdit()
        form.addRow('报告标题', self.report_title)
        form.addRow('报告代码（逗号分隔）', self.symbols)
        form.addRow('报告输出目录', self.output)
        form.addRow('报告附加备注（原样附录）', self.notes)
        self.args = QPlainTextEdit()
        self.args.setMaximumHeight(90)
        form.addRow('其他技能参数（JSON）', self.args)
        self.bind_note = QLabel()
        self.bind_note.setWordWrap(True)
        root.addWidget(self.bind_note)
        bind = QPushButton('配置眼势绑定…（不会自动修改已有绑定）')
        bind.clicked.connect(self._bind)
        root.addWidget(bind)
        row = QHBoxLayout()
        root.addLayout(row)
        for label, action in [('保存并生效', self.save), ('保存并立即执行测试', self.test), ('打开最近结果', self.open_result)]:
            button = QPushButton(label)
            button.clicked.connect(action)
            row.addWidget(button)
        self.status = QLabel('测试会真实执行所选任务；股市报告会生成 DOCX 并打开 Pages。')
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.status)
        self.event_received.connect(self._event)
        self._unsubscribe = bus.subscribe('brain.action', self.event_received.emit)
        self.destroyed.connect(lambda: self._unsubscribe())
        self.app.currentTextChanged.connect(self.load)
        self.skill.currentTextChanged.connect(self._skill_changed)
        self.app.setCurrentText('com.apple.iWork.Pages')
        self.load()

    def _skill_changed(self, *args):
        report = self.skill.currentText() == 'pages_stock_report'
        for widget in (self.report_title, self.symbols, self.output, self.notes):
            widget.setEnabled(report)
        self.instruction.setEnabled(self.skill.currentText() == 'agent')

    def _bindings(self):
        bound = [key for key, spec in self.cfg.section('eyes.gestures').items()
                 if isinstance(spec, dict) and spec.get('action') == 'brain_confirm']
        self.bind_note.setText('Brain 确认眼势：' + (', '.join(bound) if bound else '尚未绑定，请点牌子或按空格确认。'))

    def _bind(self):
        from .bind import BindDialog
        BindDialog(self.cfg, self.bus, self).exec()
        self._bindings()

    def load(self, *unused):
        p = resolve(self.cfg).get(self.app.currentText(), {})
        self.source.setText(p.get('source', '新任务'))
        self.title.setText(p.get('title', ''))
        self.skill.setCurrentText(p.get('skill') or 'agent')
        self.instruction.setPlainText(p.get('instruction', ''))
        self.confirmation.setCurrentIndex(1 if p.get('confirmation') == 'click' else 0)
        args = dict(p.get('args', {}))
        self.report_title.setText(args.pop('report_title', '今日股票报告'))
        symbols = args.pop('symbols', _DEFAULT_SYMBOLS)
        self.symbols.setText(','.join(s if isinstance(s, str) else s[0] for s in symbols))
        self.output.setText(args.pop('output_dir', '~/.supermouse/reports'))
        self.notes.setText(args.pop('notes', ''))
        self.args.setPlainText(json.dumps(args, ensure_ascii=False, indent=2))
        self._skill_changed()
        self._bindings()

    def save(self):
        try:
            args = json.loads(self.args.toPlainText() or '{}')
            if not isinstance(args, dict):
                raise ValueError('参数必须是 JSON 对象')
            if self.skill.currentText() == 'pages_stock_report':
                import re
                symbols = [s.strip() for s in self.symbols.text().replace('，', ',').split(',') if s.strip()]
                if not symbols or any(not re.fullmatch(r's_(sh|sz)\d{6}', s) for s in symbols):
                    raise ValueError('请输入简化行情代码，如 s_sh000001')
                if not self.output.text().strip() or not self.report_title.text().strip():
                    raise ValueError('报告标题与输出目录不能为空')
                args.update(symbols=symbols, report_title=self.report_title.text(),
                            output_dir=self.output.text(), notes=self.notes.text())
            plan = dict(title=self.title.text().strip(), skill=self.skill.currentText(), args=args,
                        instruction=self.instruction.toPlainText(), confirmation=self.confirmation.currentData(),
                        steps=['执行已保存任务', '检查执行结果'], needs_confirm=True)
            if plan['skill'] == 'agent' and not plan['instruction'].strip():
                raise ValueError('请填写 AI 任务要求')
            save_custom(self.cfg, self.app.currentText().strip(), plan)
            self.bus.publish('brain.settings_changed')
            self.source.setText('自定义任务')
            self.status.setText('已保存并立即生效；没有执行任务，也没有修改眼势绑定。')
            return True
        except (ValueError, OSError, TypeError) as exc:
            self.status.setText(f'保存失败：{exc}')
            return False

    def test(self):
        if self.save():
            self.artifact = ''
            self.bus.publish('brain.test', app=self.app.currentText().strip())

    def _event(self, evt):
        self.status.setText(f"{evt.get('status', '')} · {evt.get('message', '')}" +
                            (f"\n{evt['artifact_path']}" if evt.get('artifact_path') else ''))
        if evt.get('artifact_path'):
            self.artifact = evt['artifact_path']

    def open_result(self):
        if self.artifact and Path(self.artifact).is_file():
            if not macos.open_path(self.artifact):
                self.status.setText('文件打开失败：' + self.artifact)
        else:
            self.status.setText('还没有生成可打开的结果文件。')
