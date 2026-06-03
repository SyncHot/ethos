#!/bin/bash
TOKEN=$(curl -s --max-time 5 -X POST http://localhost:9000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "nasadmin", "password": "Ethos123!"}' | sed 's/.*"token":"\([^"]*\)".*/\1/')

echo ""
echo "=== Testing File Operations ==="

# Test chmod
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/chmod \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin", "mode": "755"}')
echo "--- /api/files/chmod ---"
echo "$RESP"

# Test compress
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/compress \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sources": ["/home/nasadmin"], "dest": "/home/nasadmin/test.zip"}')
echo ""
echo "--- /api/files/compress ---"
echo "$RESP"

# Test extract
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/extract \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin/test.zip", "dest": "/home/nasadmin"}')
echo ""
echo "--- /api/files/extract ---"
echo "$RESP"

# Test trash preview with ID
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/trash/preview?id=1 \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/trash/preview ---"
echo "$RESP"

# Test sync upload
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/sync/upload \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/upload ---"
echo "$RESP"

# Test sync check
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/sync/check \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin"}')
echo ""
echo "--- /api/sync/check ---"
echo "$RESP"

# Test sync reset
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/sync/reset \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/reset ---"
echo "$RESP"

# Test duplicates scan
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/scan \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paths": ["/home/nasadmin"]}')
echo ""
echo "--- /api/files/duplicates/scan ---"
echo "$RESP"

# Test duplicates cancel
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/cancel \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/cancel ---"
echo "$RESP"

# Test code editor install
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/code-editor/install \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/code-editor/install ---"
echo "$RESP"

# Test remote transfer
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/transfer-remote \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"server": "test", "source": "/tmp", "dest": "/home/nasadmin"}')
echo ""
echo "--- /api/files/transfer-remote ---"
echo "$RESP"

# Test folder password POST
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/folder-password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin", "password": "test123"}')
echo ""
echo "--- /api/files/folder-password (POST) ---"
echo "$RESP"

# Test folder password DELETE
RESP=$(curl -s --max-time 3 -X DELETE http://localhost:9000/api/files/folder-password \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"path": "/home/nasadmin"}')
echo ""
echo "--- /api/files/folder-password (DELETE) ---"
echo "$RESP"

# Test ACL PUT
RESP=$(curl -s --max-time 3 -X PUT http://localhost:9000/api/files/acl \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin", "acl": []}')
echo ""
echo "--- /api/files/acl (PUT) ---"
echo "$RESP"

# Test trash restore
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/trash/restore \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paths": []}')
echo ""
echo "--- /api/files/trash/restore ---"
echo "$RESP"

# Test trash empty
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/trash/empty \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/trash/empty ---"
echo "$RESP"

# Test trash delete
RESP=$(curl -s --max-time 3 -X DELETE http://localhost:9000/api/files/trash/delete \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"paths": []}')
echo ""
echo "--- /api/files/trash/delete ---"
echo "$RESP"

# Test folder unlock
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/folder-unlock \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin", "password": "test"}')
echo ""
echo "--- /api/files/folder-unlock ---"
echo "$RESP"

# Test folder lock
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/folder-lock \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"path": "/home/nasadmin"}')
echo ""
echo "--- /api/files/folder-lock ---"
echo "$RESP"

# Test cancel operation
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/cancel-operation \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "bg"}')
echo ""
echo "--- /api/files/cancel-operation ---"
echo "$RESP"

# Test pause operation
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/pause-operation \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "bg"}')
echo ""
echo "--- /api/files/pause-operation ---"
echo "$RESP"

# Test move multi
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/move-multi \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sources": ["/home/nasadmin/renamed_dir"], "dest": "/home/nasadmin"}')
echo ""
echo "--- /api/files/move-multi ---"
echo "$RESP"

# Test check conflicts
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/check-conflicts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sources": ["/home/nasadmin"], "dest": "/home/nasadmin"}')
echo ""
echo "--- /api/files/check-conflicts ---"
echo "$RESP"

# Test duplicates pkg-status
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/duplicates/pkg-status \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/pkg-status ---"
echo "$RESP"

# Test code-editor pkg-status
RESP=$(curl -s --max-time 3 http://localhost:9000/api/code-editor/pkg-status \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/code-editor/pkg-status ---"
echo "$RESP"

# Test duplicates install
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/install \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/install ---"
echo "$RESP"

# Test duplicates uninstall
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/uninstall \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/uninstall ---"
echo "$RESP"

# Test code-editor uninstall
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/code-editor/uninstall \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/code-editor/uninstall ---"
echo "$RESP"

# Test sync config POST
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/sync/config \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"dest": "/home/nasadmin/Phone"}')
echo ""
echo "--- /api/sync/config (POST) ---"
echo "$RESP"

# Test sync folders
RESP=$(curl -s --max-time 3 http://localhost:9000/api/sync/folders?path=/home \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/folders ---"
echo "$RESP"

# Test sync QR code
RESP=$(curl -s --max-time 3 http://localhost:9000/api/sync/qr-code?url=http://test.com \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/qr-code ---"
echo "$RESP" | head -c 50

# Test remote servers
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/remote-servers \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/remote-servers ---"
echo "$RESP"

# Test duplicates results
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/duplicates/results \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/results ---"
echo "$RESP"

# Test duplicates ignored
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/duplicates/ignored \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/ignored ---"
echo "$RESP"

# Test duplicates unignore
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/unignore \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paths": []}')
echo ""
echo "--- /api/files/duplicates/unignore ---"
echo "$RESP"

# Test duplicates ignore
RESP=$(curl -s --max-time 3 -X POST http://localhost:9000/api/files/duplicates/ignore \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"paths": []}')
echo ""
echo "--- /api/files/duplicates/ignore ---"
echo "$RESP"

# Test favorites
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/favorites \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/favorites ---"
echo "$RESP"

# Test permissions
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/permissions?path=/home/nasadmin \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/permissions ---"
echo "$RESP" | head -c 100

# Test folder password GET
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/folder-password?path=/home/nasadmin \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/folder-password (GET) ---"
echo "$RESP"

# Test ACL GET
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/acl?path=/home/nasadmin \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/acl (GET) ---"
echo "$RESP"

# Test operation status
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/operation-status \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/operation-status ---"
echo "$RESP" | head -c 100

# Test trash
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/trash \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/trash ---"
echo "$RESP"

# Test sync status
RESP=$(curl -s --max-time 3 http://localhost:9000/api/sync/status \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/status ---"
echo "$RESP" | head -c 100

# Test sync config GET
RESP=$(curl -s --max-time 3 http://localhost:9000/api/sync/config \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/sync/config (GET) ---"
echo "$RESP" | head -c 100

# Test duplicates status
RESP=$(curl -s --max-time 3 http://localhost:9000/api/files/duplicates/status \
  -H "Authorization: Bearer $TOKEN")
echo ""
echo "--- /api/files/duplicates/status ---"
echo "$RESP"

echo ""
echo "=== Container Errors ==="
docker logs ethos-server --tail 200 2>&1 | grep -E "ERROR|Traceback|NameError|ImportError" | tail -20 || echo "No errors found"
