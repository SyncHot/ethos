"""
File→App mapping — determines which visual tests to run based on changed files.
"""

import re
import os
import logging

log = logging.getLogger("visual_qa.file_app_map")

_FILE_APP_RULES = [
    (r'frontend/js/apps/dashboard\.js', ['dashboard']),
    (r'frontend/js/apps/tickets\.js', ['tickets']),
    (r'frontend/js/apps/storage\.js', ['storage']),
    (r'frontend/js/apps/aichat\.js', ['ai-chat']),
    (r'frontend/js/apps/backup\.js', ['backup']),
    (r'frontend/js/apps/gallery\.js', ['gallery']),
    (r'frontend/js/apps/surveillance\.js', ['surveillance']),
    (r'frontend/js/apps/network\.js', ['network']),
    (r'frontend/js/apps/(\w+)\.js', None),  # derive from filename
    (r'frontend/js/apps\.js', ['file-manager', 'dashboard', 'docker-manager']),
    (r'frontend/js/desktop\.js', ['_desktop']),
    (r'frontend/js/i18n\.js', ['dashboard']),
    (r'frontend/css/style\.css', ['_all']),
    (r'frontend/css/apps\.css', ['_all']),
    (r'frontend/css/apps/', ['_all']),
    (r'frontend/index\.html', ['_desktop']),
]

ALL_MAIN_APPS = ['dashboard', 'tickets', 'file-manager', 'storage', 'ai-chat']


def get_affected_apps(changed_files):
    """
    Given changed file paths, return AppRegistry IDs to visually test.
    Special: _desktop = desktop shell, _all = ALL_MAIN_APPS.
    """
    apps = set()
    for fpath in changed_files:
        fpath = fpath.strip().lstrip('/')
        if not fpath.startswith('frontend/'):
            continue
        for pattern, app_ids in _FILE_APP_RULES:
            m = re.search(pattern, fpath)
            if m:
                if app_ids is None:
                    name = m.group(1) if m.lastindex else os.path.splitext(os.path.basename(fpath))[0]
                    apps.add(re.sub(r'_', '-', name))
                else:
                    apps.update(app_ids)
                break
    if '_all' in apps:
        apps.discard('_all')
        apps.update(ALL_MAIN_APPS)
    return apps
