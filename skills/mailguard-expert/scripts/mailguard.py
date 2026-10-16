#!/usr/bin/env python3
"""Canonical launcher copied into the self-contained Skill at build time."""
from pathlib import Path
import json
import sys

sys.dont_write_bytecode = True
SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL/'scripts/_runtime'))
try:
    from mailguard.bootstrap import skill_main
except (ImportError, SyntaxError):
    print(json.dumps({'status': 'error', 'error_code': 'bootstrap_unavailable',
                      'error': 'Skill引导程序不完整或Python版本不支持。',
                      'recovery': {'automatic_retry': False, 'next_steps': [
                          '确认使用Python3.12+；重新安装完整Skill，保留工作区.mailguard中的配置和记录。']}}, ensure_ascii=False))
    raise SystemExit(1) from None

if __name__ == '__main__':
    raise SystemExit(skill_main(SKILL))
