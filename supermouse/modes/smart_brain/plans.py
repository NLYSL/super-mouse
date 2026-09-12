"""One resolver for hover, settings and manual tests. Never execute plans on save."""
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from .skills import DEFAULT_PLANS, REGISTRY

log = logging.getLogger(__name__)
META = {'title', 'skill', 'args', 'steps', 'needs_confirm', 'confirmation', 'instruction', 'source'}


def validate(app, plan):
    if not isinstance(app, str) or not re.fullmatch(r'[\w-]+(?:\.[\w-]+)+', app):
        raise ValueError('请输入应用 Bundle ID，例如 com.apple.iWork.Pages')
    if not isinstance(plan, dict) or not isinstance(plan.get('title'), str) or not plan['title'].strip():
        raise ValueError('任务名称不能为空')
    if plan.get('skill') not in {*REGISTRY, 'agent', None, ''}:
        raise ValueError('未知技能；自定义 AI 任务请选择 agent')
    if not isinstance(plan.get('args', {}), dict):
        raise ValueError('技能参数必须是 JSON 对象')
    if not isinstance(plan.get('steps', []), list) or any(not isinstance(s, str) for s in plan.get('steps', [])):
        raise ValueError('步骤必须是字符串列表')
    if plan.get('confirmation', 'any') not in ('any', 'click'):
        raise ValueError('确认方式无效')
    if not isinstance(plan.get('instruction', ''), str):
        raise ValueError('任务要求必须是文本')
    return deepcopy(plan)


def custom_path(cfg):
    return cfg.path_of('brain.knowledge_dir', '~/.supermouse/brain') / 'plans.json'


def read_custom(cfg):
    path = custom_path(cfg)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('plans.json 必须是应用 ID 到计划的对象')
    return data


def resolve(cfg):
    plans = {app: {**deepcopy(p), 'source': '内置预设'} for app, p in DEFAULT_PLANS.items()}
    for app, entry in cfg.section('brain.apps').items():
        if not isinstance(entry, dict):
            continue
        base = deepcopy(plans.get(app, {'title': f'执行 {app} 任务'}))
        if entry.get('skill', base.get('skill')) != base.get('skill'):
            base['args'] = {}
        try:
            configured_args = entry.get('args', {})
            if not isinstance(configured_args, dict):
                raise ValueError('args 必须是对象')
            args = {**base.get('args', {}), **{k: v for k, v in entry.items() if k not in META}, **configured_args}
            base.update({k: v for k, v in entry.items() if k in META})
            base.update(args=args, source='应用配置')
            plans[app] = validate(app, base)
        except ValueError as exc:
            log.warning('忽略无效应用配置 %s: %s', app, exc)
    try:
        for app, entry in read_custom(cfg).items():
            try:
                plans[app] = {**validate(app, entry), 'source': '自定义任务'}
            except ValueError as exc:
                log.warning('忽略无效计划 %s: %s', app, exc)
    except (OSError, ValueError) as exc:
        log.warning('读取自定义计划失败: %s', exc)
    return plans


def save_custom(cfg, app, plan):
    plan = validate(app, plan)
    plan.pop('source', None)
    data = read_custom(cfg)  # malformed existing file: fail, never overwrite it
    data[app] = plan
    path = custom_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.plans-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
