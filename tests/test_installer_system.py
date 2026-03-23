"""Tests for system operations (mocked commands)."""

import sys
import os
import tempfile
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "installer", "preboot"))

import system_ops


@patch("system_ops._run")
def test_create_user_new(mock_run):
    mock_run.side_effect = [
        ("", "", 1),   # id — not found
        ("", "", 0),   # useradd
        ("", "", 0),   # chpasswd
    ]
    assert system_ops.create_user("testuser", "pass123") is True
    calls = [str(c) for c in mock_run.call_args_list]
    assert any("useradd" in c for c in calls)


@patch("system_ops._run")
def test_create_user_existing(mock_run):
    mock_run.side_effect = [
        ("uid=1000", "", 0),  # id — exists
        ("", "", 0),          # chpasswd
    ]
    assert system_ops.create_user("testuser", "newpass") is True


def test_write_install_conf():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "opt/ethos"))
        system_ops.write_install_conf("admin", "myhost", 9000, root_dir=root)
        content = open(os.path.join(root, "opt/ethos/install.conf")).read()
        assert 'ETHOS_USER="admin"' in content
        assert 'ETHOS_HOSTNAME="myhost"' in content
        assert "ETHOS_SETUP_WIZARD=no" in content


def test_mark_installed():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "opt/ethos"))
        system_ops.mark_installed(root_dir=root)
        assert os.path.exists(os.path.join(root, "opt/ethos/.installed"))


def test_set_hostname():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "root")
        os.makedirs(os.path.join(root, "etc"))
        with open(os.path.join(root, "etc/hosts"), "w") as f:
            f.write("127.0.0.1 localhost\n")
        system_ops.set_hostname("my-nas", root_dir=root)
        assert open(os.path.join(root, "etc/hostname")).read().strip() == "my-nas"
        assert "my-nas" in open(os.path.join(root, "etc/hosts")).read()
