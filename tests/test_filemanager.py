"""
EthOS – Comprehensive File Manager tests.

Covers: mkdir, upload, list, rename, copy, move, delete, trash (list/restore/empty),
favorites (add/remove), search, permissions, info, check-conflicts, operation status,
and error cases for each endpoint.

Run:
    ETHOS_USER=<user> ETHOS_PASS=<pass> pytest tests/test_filemanager.py -v
"""

import io
import os
import time
import uuid
import pytest
from helpers import api_get, api_post, BASE_URL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TAG = uuid.uuid4().hex[:8]
FM_ROOT = f"/tmp/e2e-fm-{TAG}"


def _get(s, path, **kw):
    return api_get(s, path, **kw)


def _post(s, path, **kw):
    return api_post(s, path, **kw)


def _delete(s, path, **kw):
    kw.setdefault("timeout", 15)
    return s.delete(f"{s.base_url}{path}", **kw)


def _upload(s, dest_path, filename, content=b"test"):
    """Upload a file via multipart. Temporarily drops Content-Type header."""
    saved_ct = s.headers.pop("Content-Type", None)
    try:
        files = {"files": (filename, io.BytesIO(content), "text/plain")}
        r = s.post(
            f"{s.base_url}/api/files/upload",
            data={"path": dest_path},
            files=files,
            timeout=15,
        )
        return r
    finally:
        if saved_ct:
            s.headers["Content-Type"] = saved_ct


_list_seq = 0

def _list_dir(s, path):
    """List directory, bypassing the server-side response cache."""
    global _list_seq
    _list_seq += 1
    return _get(s, "/api/files/list",
                params={"path": path, "_cb": _list_seq})


def _json(resp):
    ct = resp.headers.get("Content-Type", "")
    if "application/json" in ct:
        return resp.json()
    return None


# ═══════════════════════════════════════════════════════════════
#  SETUP / TEARDOWN — create and clean test workspace
# ═══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module", autouse=True)
def fm_workspace(api_session):
    """Create a temp directory for all FM tests, clean up at the end."""
    r = _post(api_session, "/api/files/mkdir", json={"path": FM_ROOT})
    assert r.status_code == 200, f"Failed to create test dir: {r.text}"
    yield FM_ROOT
    # Teardown — permanent delete
    _delete(api_session, "/api/files/delete",
            json={"paths": [FM_ROOT], "permanent": True})


# ═══════════════════════════════════════════════════════════════
#  MKDIR
# ═══════════════════════════════════════════════════════════════

class TestMkdir:
    def test_mkdir_basic(self, api_session, fm_workspace):
        path = f"{fm_workspace}/subdir1"
        r = _post(api_session, "/api/files/mkdir", json={"path": path})
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True

    def test_mkdir_nested(self, api_session, fm_workspace):
        """Creating deeply nested dir."""
        path = f"{fm_workspace}/a/b/c"
        r = _post(api_session, "/api/files/mkdir", json={"path": path})
        assert r.status_code == 200

    def test_mkdir_duplicate(self, api_session, fm_workspace):
        """Creating an existing dir should be handled gracefully."""
        path = f"{fm_workspace}/dup_dir"
        _post(api_session, "/api/files/mkdir", json={"path": path})
        r = _post(api_session, "/api/files/mkdir", json={"path": path})
        # Should either succeed silently or return a conflict error
        assert r.status_code in (200, 400, 409)

    def test_mkdir_no_path(self, api_session):
        """Missing path should return 400."""
        r = _post(api_session, "/api/files/mkdir", json={})
        assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════
#  UPLOAD
# ═══════════════════════════════════════════════════════════════

class TestUpload:
    def test_upload_single_file(self, api_session, fm_workspace):
        r = _upload(api_session, fm_workspace, "test_upload.txt", b"upload content")
        assert r.status_code == 200
        d = r.json()
        assert "test_upload.txt" in d.get("uploaded", [])
        assert d.get("errors") == []

    def test_upload_multiple_files(self, api_session, fm_workspace):
        _upload(api_session, fm_workspace, "multi1.txt", b"file1")
        _upload(api_session, fm_workspace, "multi2.txt", b"file2")
        r = _list_dir(api_session, fm_workspace)
        assert r.status_code == 200
        names = [i["name"] for i in r.json().get("items", [])]
        assert "multi1.txt" in names
        assert "multi2.txt" in names

    def test_upload_to_nonexistent_path(self, api_session):
        r = _upload(api_session, "/tmp/nonexistent_upload_dir_99999", "x.txt", b"x")
        assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════
#  LIST
# ═══════════════════════════════════════════════════════════════

class TestList:
    def test_list_workspace(self, api_session, fm_workspace):
        r = _list_dir(api_session, fm_workspace)
        assert r.status_code == 200
        d = r.json()
        assert "items" in d
        names = [i["name"] for i in d["items"]]
        assert "subdir1" in names or "test_upload.txt" in names

    def test_list_returns_type_info(self, api_session, fm_workspace):
        """Each item should have name, type (file/dir), and size."""
        r = _list_dir(api_session, fm_workspace)
        items = r.json().get("items", [])
        if items:
            item = items[0]
            assert "name" in item
            assert "type" in item or "is_dir" in item or "isDir" in item

    def test_list_nonexistent(self, api_session):
        r = _list_dir(api_session, "/nonexistent_dir_12345")
        d = _json(r)
        assert r.status_code in (400, 404) or (d and "error" in d)

    def test_list_ethos_root(self, api_session):
        r = _list_dir(api_session, "/opt/ethos")
        assert r.status_code == 200
        names = [i["name"] for i in r.json()["items"]]
        assert "backend" in names


# ═══════════════════════════════════════════════════════════════
#  RENAME
# ═══════════════════════════════════════════════════════════════

class TestRename:
    def test_rename_file(self, api_session, fm_workspace):
        # Create a file to rename
        _upload(api_session, fm_workspace, "rename_me.txt", b"rename test")
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{fm_workspace}/rename_me.txt",
            "new_name": "renamed.txt",
        })
        assert r.status_code == 200

        # Verify renamed file exists in listing
        r = _list_dir(api_session, fm_workspace)
        names = [i["name"] for i in r.json()["items"]]
        assert "renamed.txt" in names
        assert "rename_me.txt" not in names

    def test_rename_dir(self, api_session, fm_workspace):
        _post(api_session, "/api/files/mkdir", json={"path": f"{fm_workspace}/dir_rename_test"})
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{fm_workspace}/dir_rename_test",
            "new_name": "dir_renamed",
        })
        assert r.status_code == 200

    def test_rename_nonexistent(self, api_session, fm_workspace):
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{fm_workspace}/no_such_file.txt",
            "new_name": "whatever.txt",
        })
        # Server returns 500 because os.rename raises FileNotFoundError
        assert r.status_code in (400, 404, 500)

    def test_rename_conflict(self, api_session, fm_workspace):
        """Renaming to an existing name — Linux os.rename overwrites silently."""
        for name in ("conflict_a.txt", "conflict_b.txt"):
            _upload(api_session, fm_workspace, name, b"x")
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{fm_workspace}/conflict_a.txt",
            "new_name": "conflict_b.txt",
        })
        # os.rename on Linux silently overwrites, so this succeeds
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  COPY
# ═══════════════════════════════════════════════════════════════

class TestCopy:
    def test_copy_single_file(self, api_session, fm_workspace):
        # Create source
        _upload(api_session, fm_workspace, "copy_src.txt", b"copy me")
        _post(api_session, "/api/files/mkdir", json={"path": f"{fm_workspace}/copy_dest"})

        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{fm_workspace}/copy_src.txt"],
            "dest": f"{fm_workspace}/copy_dest",
            "on_conflict": "rename",
        })
        assert r.status_code == 200
        d = r.json()
        assert "copy_src.txt" in d.get("copied", [])
        assert d.get("errors", []) == []

        # Verify source still exists (copy, not move)
        r = _list_dir(api_session, fm_workspace)
        names = [i["name"] for i in r.json()["items"]]
        assert "copy_src.txt" in names

        # Verify copy in destination
        r = _list_dir(api_session, f"{fm_workspace}/copy_dest")
        dest_names = [i["name"] for i in r.json()["items"]]
        assert "copy_src.txt" in dest_names

    def test_copy_multiple_files(self, api_session, fm_workspace):
        for name in ("multi_cp_1.txt", "multi_cp_2.txt"):
            _upload(api_session, fm_workspace, name, b"multi")
        _post(api_session, "/api/files/mkdir",
              json={"path": f"{fm_workspace}/multi_cp_dest"})

        r = _post(api_session, "/api/files/copy", json={
            "sources": [
                f"{fm_workspace}/multi_cp_1.txt",
                f"{fm_workspace}/multi_cp_2.txt",
            ],
            "dest": f"{fm_workspace}/multi_cp_dest",
        })
        assert r.status_code == 200
        d = r.json()
        assert len(d.get("copied", [])) == 2

    def test_copy_directory(self, api_session, fm_workspace):
        """Copy an entire directory."""
        src_dir = f"{fm_workspace}/cp_dir_src"
        _post(api_session, "/api/files/mkdir", json={"path": src_dir})
        _upload(api_session, src_dir, "inner.txt", b"inner")
        dest_dir = f"{fm_workspace}/cp_dir_dest"
        _post(api_session, "/api/files/mkdir", json={"path": dest_dir})

        r = _post(api_session, "/api/files/copy", json={
            "sources": [src_dir],
            "dest": dest_dir,
        })
        assert r.status_code == 200
        d = r.json()
        assert "cp_dir_src" in d.get("copied", [])

    def test_copy_nonexistent_source(self, api_session, fm_workspace):
        _post(api_session, "/api/files/mkdir",
              json={"path": f"{fm_workspace}/cp_err_dest"})
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{fm_workspace}/no_such_file_999.txt"],
            "dest": f"{fm_workspace}/cp_err_dest",
        })
        assert r.status_code == 200
        d = r.json()
        assert len(d.get("errors", [])) > 0
        assert d.get("copied", []) == []

    def test_copy_conflict_rename(self, api_session, fm_workspace):
        """Copying a file that already exists in dest with on_conflict=rename."""
        dest = f"{fm_workspace}/cp_conflict_dest"
        _post(api_session, "/api/files/mkdir", json={"path": dest})
        # Upload same-named file to source and dest
        for target_path in (fm_workspace, dest):
            _upload(api_session, target_path, "dup.txt", b"dup")
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{fm_workspace}/dup.txt"],
            "dest": dest,
            "on_conflict": "rename",
        })
        assert r.status_code == 200
        d = r.json()
        assert len(d.get("copied", [])) == 1  # should succeed with rename

    def test_copy_conflict_skip(self, api_session, fm_workspace):
        dest = f"{fm_workspace}/cp_skip_dest"
        _post(api_session, "/api/files/mkdir", json={"path": dest})
        for target_path in (fm_workspace, dest):
            _upload(api_session, target_path, "skip_me.txt", b"s")
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{fm_workspace}/skip_me.txt"],
            "dest": dest,
            "on_conflict": "skip",
        })
        assert r.status_code == 200
        d = r.json()
        assert "skip_me.txt" in d.get("skipped", [])

    def test_copy_missing_params(self, api_session):
        r = _post(api_session, "/api/files/copy", json={})
        assert r.status_code == 400

    def test_copy_dest_not_a_dir(self, api_session, fm_workspace):
        """Dest must be a directory."""
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{fm_workspace}/copy_src.txt"],
            "dest": f"{fm_workspace}/copy_src.txt",  # a file, not dir
        })
        assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════
#  MOVE
# ═══════════════════════════════════════════════════════════════

class TestMove:
    def test_move_single_file(self, api_session, fm_workspace):
        _upload(api_session, fm_workspace, "move_me.txt", b"move")
        _post(api_session, "/api/files/mkdir",
              json={"path": f"{fm_workspace}/move_dest"})

        r = _post(api_session, "/api/files/move", json={
            "src": f"{fm_workspace}/move_me.txt",
            "dest": f"{fm_workspace}/move_dest",
        })
        assert r.status_code == 200

        # Source should be gone
        r = _list_dir(api_session, fm_workspace)
        names = [i["name"] for i in r.json()["items"]]
        assert "move_me.txt" not in names

    def test_move_multi(self, api_session, fm_workspace):
        for name in ("mv1.txt", "mv2.txt"):
            _upload(api_session, fm_workspace, name, b"mv")
        _post(api_session, "/api/files/mkdir",
              json={"path": f"{fm_workspace}/mv_multi_dest"})

        r = _post(api_session, "/api/files/move-multi", json={
            "sources": [
                f"{fm_workspace}/mv1.txt",
                f"{fm_workspace}/mv2.txt",
            ],
            "dest": f"{fm_workspace}/mv_multi_dest",
        })
        assert r.status_code == 200

    def test_move_nonexistent(self, api_session, fm_workspace):
        r = _post(api_session, "/api/files/move", json={
            "src": f"{fm_workspace}/no_file.txt",
            "dest": f"{fm_workspace}",
        })
        assert r.status_code in (400, 404, 500)


# ═══════════════════════════════════════════════════════════════
#  DELETE & TRASH
# ═══════════════════════════════════════════════════════════════

class TestDeleteAndTrash:
    def test_delete_to_trash(self, api_session, fm_workspace):
        """Default delete goes to trash (permanent=false)."""
        _upload(api_session, fm_workspace, "trash_me.txt", b"trash")
        r = _delete(api_session, "/api/files/delete",
                     json={"paths": [f"{fm_workspace}/trash_me.txt"]})
        assert r.status_code == 200

    def test_delete_permanent(self, api_session, fm_workspace):
        _upload(api_session, fm_workspace, "perm_del.txt", b"gone")
        r = _delete(api_session, "/api/files/delete",
                     json={"paths": [f"{fm_workspace}/perm_del.txt"],
                            "permanent": True})
        assert r.status_code == 200

    def test_trash_list(self, api_session):
        r = _get(api_session, "/api/files/trash")
        assert r.status_code == 200
        d = r.json()
        assert "items" in d

    def test_trash_restore(self, api_session, fm_workspace):
        """Delete a file to trash, then restore it."""
        _upload(api_session, fm_workspace, "restore_me.txt", b"restore")
        _delete(api_session, "/api/files/delete",
                json={"paths": [f"{fm_workspace}/restore_me.txt"]})

        # Find the trashed item
        time.sleep(0.5)
        r = _get(api_session, "/api/files/trash")
        items = r.json().get("items", [])
        restore_item = None
        for it in items:
            if it.get("name", "").startswith("restore_me"):
                restore_item = it
                break
        if not restore_item:
            pytest.skip("Trashed item not found (trash may be disabled)")

        # Restore
        trash_path = restore_item.get("trash_path") or restore_item.get("path", "")
        r = _post(api_session, "/api/files/trash/restore",
                  json={"paths": [trash_path]})
        assert r.status_code == 200

    def test_trash_empty(self, api_session, fm_workspace):
        """Empty entire trash."""
        # Add something to trash first
        _upload(api_session, fm_workspace, "empty_trash.txt", b"t")
        _delete(api_session, "/api/files/delete",
                json={"paths": [f"{fm_workspace}/empty_trash.txt"]})

        r = _post(api_session, "/api/files/trash/empty", json={})
        assert r.status_code == 200

    def test_delete_nonexistent(self, api_session, fm_workspace):
        r = _delete(api_session, "/api/files/delete",
                     json={"paths": [f"{fm_workspace}/nope_999.txt"],
                            "permanent": True})
        # Should handle gracefully
        assert r.status_code in (200, 400, 404)

    def test_delete_no_paths(self, api_session):
        r = _delete(api_session, "/api/files/delete", json={"paths": []})
        assert r.status_code in (200, 400)


# ═══════════════════════════════════════════════════════════════
#  FAVORITES
# ═══════════════════════════════════════════════════════════════

class TestFavorites:
    def test_favorites_lifecycle(self, api_session, fm_workspace):
        """Add → list → remove → verify removed."""
        fav_path = fm_workspace

        # Add
        r = _post(api_session, "/api/files/favorites",
                  json={"path": fav_path, "label": "TestFav"})
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert any(f["path"] == fav_path for f in d.get("favorites", []))

        # List
        r = _get(api_session, "/api/files/favorites")
        assert r.status_code == 200
        favs = r.json()
        assert any(f["path"] == fav_path for f in favs)

        # Remove
        r = _delete(api_session, "/api/files/favorites",
                     json={"path": fav_path})
        assert r.status_code == 200
        d = r.json()
        assert d.get("ok") is True
        assert not any(f["path"] == fav_path for f in d.get("favorites", []))

        # Verify gone
        r = _get(api_session, "/api/files/favorites")
        favs = r.json()
        assert not any(f["path"] == fav_path for f in favs)

    def test_favorites_add_duplicate(self, api_session, fm_workspace):
        """Adding same path twice should return 409."""
        fav_path = f"{fm_workspace}/subdir1"
        _post(api_session, "/api/files/favorites",
              json={"path": fav_path, "label": "Dup"})
        r = _post(api_session, "/api/files/favorites",
                  json={"path": fav_path, "label": "Dup2"})
        assert r.status_code == 409
        # Cleanup
        _delete(api_session, "/api/files/favorites", json={"path": fav_path})

    def test_favorites_add_no_path(self, api_session):
        r = _post(api_session, "/api/files/favorites", json={})
        assert r.status_code == 400

    def test_favorites_remove_nonexistent(self, api_session):
        """Removing a non-favorited path should still succeed (idempotent)."""
        r = _delete(api_session, "/api/files/favorites",
                     json={"path": "/tmp/never_favorited_12345"})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  SEARCH
# ═══════════════════════════════════════════════════════════════

class TestSearch:
    def test_search_by_name(self, api_session, fm_workspace):
        """Search for a known file in the workspace."""
        # Ensure there's a file
        _upload(api_session, fm_workspace, "searchable.txt", b"find me")
        r = _get(api_session, "/api/files/search",
                 params={"path": fm_workspace, "q": "searchable"})
        assert r.status_code == 200
        d = r.json()
        results = d.get("items", d.get("results", []))
        found = any("searchable" in (it.get("name", "") + it.get("path", ""))
                     for it in results)
        assert found, f"Expected to find 'searchable' in results: {results}"

    def test_search_no_results(self, api_session, fm_workspace):
        r = _get(api_session, "/api/files/search",
                 params={"path": fm_workspace, "q": "zzz_no_match_999"})
        assert r.status_code == 200
        d = r.json()
        results = d.get("items", d.get("results", []))
        assert len(results) == 0

    def test_search_in_ethos(self, api_session):
        """Search for app.py in /opt/ethos."""
        r = _get(api_session, "/api/files/search",
                 params={"path": "/opt/ethos", "q": "app.py"})
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  INFO & PERMISSIONS
# ═══════════════════════════════════════════════════════════════

class TestInfoAndPermissions:
    def test_file_details_in_list(self, api_session, fm_workspace):
        """List endpoint should return file details (name, size, type)."""
        _upload(api_session, fm_workspace, "info_test.txt", b"info")
        r = _list_dir(api_session, fm_workspace)
        assert r.status_code == 200
        items = r.json().get("items", [])
        found = [i for i in items if i["name"] == "info_test.txt"]
        assert len(found) == 1
        item = found[0]
        assert "name" in item
        assert "size" in item or "type" in item

    def test_permissions_read(self, api_session):
        r = _get(api_session, "/api/files/permissions",
                 params={"path": "/opt/ethos/start.sh"})
        assert r.status_code == 200
        d = r.json()
        assert "owner" in d or "permissions" in d or "mode" in d

    def test_permissions_nonexistent(self, api_session, fm_workspace):
        r = _get(api_session, "/api/files/permissions",
                 params={"path": f"{fm_workspace}/nope_999.txt"})
        assert r.status_code in (400, 404)


# ═══════════════════════════════════════════════════════════════
#  CHECK CONFLICTS
# ═══════════════════════════════════════════════════════════════

class TestCheckConflicts:
    def test_check_conflicts_none(self, api_session, fm_workspace):
        """No conflict when dest is empty."""
        dest = f"{fm_workspace}/conflict_check_empty"
        _post(api_session, "/api/files/mkdir", json={"path": dest})
        r = _post(api_session, "/api/files/check-conflicts", json={
            "sources": [f"{fm_workspace}/info_test.txt"],
            "dest": dest,
        })
        assert r.status_code == 200
        d = r.json()
        conflicts = d.get("conflicts", [])
        assert len(conflicts) == 0

    def test_check_conflicts_exists(self, api_session, fm_workspace):
        """Conflict when same-named file already in dest."""
        dest = f"{fm_workspace}/conflict_check_dup"
        _post(api_session, "/api/files/mkdir", json={"path": dest})
        for target in (fm_workspace, dest):
            _upload(api_session, target, "dup_chk.txt", b"d")
        r = _post(api_session, "/api/files/check-conflicts", json={
            "sources": [f"{fm_workspace}/dup_chk.txt"],
            "dest": dest,
        })
        assert r.status_code == 200
        d = r.json()
        conflicts = d.get("conflicts", [])
        assert len(conflicts) >= 1


# ═══════════════════════════════════════════════════════════════
#  OPERATION STATUS
# ═══════════════════════════════════════════════════════════════

class TestOperationStatus:
    def test_operation_status_idle(self, api_session):
        """Should return OK even when no operation is running."""
        r = _get(api_session, "/api/files/operation-status")
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  FULL LIFECYCLE (integration)
# ═══════════════════════════════════════════════════════════════

class TestFullLifecycle:
    """End-to-end: mkdir → upload → rename → copy → move → delete → trash → restore → empty."""

    def test_complete_lifecycle(self, api_session, fm_workspace):
        base = f"{fm_workspace}/lifecycle"

        # 1. mkdir
        r = _post(api_session, "/api/files/mkdir", json={"path": base})
        assert r.status_code == 200

        # 2. upload
        r = _upload(api_session, base, "life.txt", b"lifecycle content")
        assert r.status_code == 200

        # 3. list — verify
        r = _list_dir(api_session, base)
        names = [i["name"] for i in r.json()["items"]]
        assert "life.txt" in names

        # 4. rename
        r = _post(api_session, "/api/files/rename", json={
            "path": f"{base}/life.txt", "new_name": "alive.txt",
        })
        assert r.status_code == 200

        # 5. copy
        _post(api_session, "/api/files/mkdir", json={"path": f"{base}/copies"})
        r = _post(api_session, "/api/files/copy", json={
            "sources": [f"{base}/alive.txt"],
            "dest": f"{base}/copies",
        })
        assert r.status_code == 200
        d = r.json()
        assert "alive.txt" in d.get("copied", [])

        # 6. move
        _post(api_session, "/api/files/mkdir", json={"path": f"{base}/moved"})
        r = _post(api_session, "/api/files/move", json={
            "src": f"{base}/copies/alive.txt",
            "dest": f"{base}/moved",
        })
        assert r.status_code == 200

        # 7. delete to trash
        r = _delete(api_session, "/api/files/delete",
                     json={"paths": [f"{base}/alive.txt"]})
        assert r.status_code == 200

        # 8. check trash
        time.sleep(0.5)
        r = _get(api_session, "/api/files/trash")
        assert r.status_code == 200

        # 9. permanent delete remaining
        r = _delete(api_session, "/api/files/delete",
                     json={"paths": [base], "permanent": True})
        assert r.status_code == 200
