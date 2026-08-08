#!/bin/bash
# Duck Agent - Backend Loader Smoke Tests
# Quick smoke tests for the backend system

set -e

DUCK_AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DUCK_AGENT_DIR"

PASS=0
FAIL=0

check() {
    local name="$1"
    local cmd="$2"
    if eval "$cmd" > /dev/null 2>&1; then
        echo "[PASS] $name"
        ((PASS++))
    else
        echo "[FAIL] $name"
        ((FAIL++))
    fi
}

echo "==============================="
echo "Duck Agent Backend Tests"
echo "==============================="
echo ""

# Test 1: Python module imports
echo "1. Python Module Tests"
echo "----------------------"
check "BackendType enum" "python3 -c 'from duck_agent.backends import BackendType'"
check "get_backend() function" "python3 -c 'from duck_agent.backends import get_backend'"
check "is_valid_backend() function" "python3 -c 'from duck_agent.backends import is_valid_backend'"
check "initialize_backend() function" "python3 -c 'from duck_agent.backends import initialize_backend'"
echo ""

# Test 2: Backend values
echo "2. Backend Value Tests"
echo "----------------------"
check "Grok Build backend" "python3 -c 'from duck_agent.backends import BackendType; assert BackendType.GROK_BUILD.value == \"grok-build\"'"
check "Hermes-Compatible backend" "python3 -c 'from duck_agent.backends import BackendType; assert BackendType.HERMES_COMPATIBLE.value == \"hermes-compatible\"'"
check "Prime Agent backend" "python3 -c 'from duck_agent.backends import BackendType; assert BackendType.PRIME_AGENT.value == \"prime-agent\"'"
echo ""

# Test 3: Backend selection
echo "3. Backend Selection Tests"
echo "--------------------------"
check "Default backend is grok-build" "python3 -c 'from duck_agent.backends import get_backend, BackendType; assert get_backend() == BackendType.GROK_BUILD'"
check "grok-build selection" "DUCK_AGENT_BACKEND=grok-build python3 -c 'from duck_agent.backends import get_backend, BackendType; assert get_backend() == BackendType.GROK_BUILD'"
check "hermes-compatible selection" "DUCK_AGENT_BACKEND=hermes-compatible python3 -c 'from duck_agent.backends import get_backend, BackendType; assert get_backend() == BackendType.HERMES_COMPATIBLE'"
check "prime-agent selection" "DUCK_AGENT_BACKEND=prime-agent python3 -c 'from duck_agent.backends import get_backend, BackendType; assert get_backend() == BackendType.PRIME_AGENT'"
check "Invalid backend falls back" "DUCK_AGENT_BACKEND=invalid python3 -c 'from duck_agent.backends import get_backend, BackendType; assert get_backend() == BackendType.GROK_BUILD'"
echo ""

# Test 4: Backend info
echo "4. Backend Info Tests"
echo "---------------------"
check "get_backend_info returns dict" "python3 -c 'from duck_agent.backends import get_backend_info; assert isinstance(get_backend_info(), dict)'"
check "All backends documented" "python3 -c 'from duck_agent.backends import get_backend_info; info = get_backend_info(); assert all(k in info for k in [\"grok-build\", \"hermes-compatible\", \"prime-agent\"])'"
check "Grok Build is recommended" "python3 -c 'from duck_agent.backends import get_backend_info; assert get_backend_info()[\"grok-build\"][\"recommended\"]'"
echo ""

# Test 5: Backend initialization
echo "5. Backend Initialization Tests"
echo "-------------------------------"
check "Initialize grok-build" "DUCK_AGENT_BACKEND=grok-build python3 -c 'from duck_agent.backends import initialize_backend; assert \"Grok Build\" in initialize_backend()'"
check "Initialize hermes-compatible" "DUCK_AGENT_BACKEND=hermes-compatible python3 -c 'from duck_agent.backends import initialize_backend; assert \"Hermes-compatible\" in initialize_backend()'"
check "Initialize prime-agent" "DUCK_AGENT_BACKEND=prime-agent python3 -c 'from duck_agent.backends import initialize_backend; assert \"Prime Agent\" in initialize_backend()'"
echo ""

# Test 6: Launcher script
echo "6. Launcher Script Tests"
echo "------------------------"
check "Launcher exists" "test -f $DUCK_AGENT_DIR/duck-agent"
check "Launcher is executable" "test -x $DUCK_AGENT_DIR/duck-agent"
check "Launcher --help works" "$DUCK_AGENT_DIR/duck-agent --help"
check "Launcher --version works" "$DUCK_AGENT_DIR/duck-agent --version"
check "Launcher --backends works" "$DUCK_AGENT_DIR/duck-agent --backends"
check "Launcher --status works" "$DUCK_AGENT_DIR/duck-agent --status"
echo ""

# Test 7: File structure
echo "7. File Structure Tests"
echo "-----------------------"
check "Desktop app exists" "test -d $DUCK_AGENT_DIR/apps/desktop"
check "Backends directory exists" "test -d $DUCK_AGENT_DIR/backends"
check "Grok Build backend exists" "test -d $DUCK_AGENT_DIR/backends/grok-build"
check "Duck Agent package exists" "test -d $DUCK_AGENT_DIR/duck_agent"
check "Duck logo exists" "test -f $DUCK_AGENT_DIR/apps/desktop/public/duck-logo.png"
check "README.md exists" "test -f $DUCK_AGENT_DIR/README.md"
check "LICENSE exists" "test -f $DUCK_AGENT_DIR/LICENSE"
echo ""

echo "==============================="
echo "SUMMARY: $PASS passed, $FAIL failed"
echo "==============================="

if [ $FAIL -eq 0 ]; then
    echo "[SUCCESS] All checks passed!"
    exit 0
else
    echo "[FAILURE] Some checks failed"
    exit 1
fi
