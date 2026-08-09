#!/bin/bash
# Duck Agent Comprehensive Test Runner
# Runs all tests for the Duck Agent project (Python + TypeScript)

# Don't exit on error - we want to run all tests
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PASS=0
FAIL=0
TOTAL_PYTHON=0
TOTAL_TS=0

echo "========================================"
echo "Duck Agent Comprehensive Test Suite"
echo "========================================"
echo ""

# 1. Python tests
echo "[1/10] Running Python unittest discovery..."
echo "-----------------------------------"
RESULT=$(python3 -m unittest discover -s tests -p 'test_*.py' 2>&1)
echo "$RESULT"
if echo "$RESULT" | grep -q "^OK$"; then
    echo "Python tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "Ran [0-9]+ tests" | grep -oE "[0-9]+")
    TOTAL_PYTHON=$((TOTAL_PYTHON+COUNT))
else
    echo "Python tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 2. TypeScript backend loader tests
echo "[2/10] Running TypeScript backend loader tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-index.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript loader tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript loader tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 3. TypeScript harness tests
echo "[3/10] Running TypeScript harness tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-harness.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript harness tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript harness tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 4. TypeScript integration tests
echo "[4/10] Running TypeScript integration tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-integration.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript integration tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript integration tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 5. TypeScript API client tests
echo "[5/10] Running TypeScript API client tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-api-client.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript API client tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript API client tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 6. TypeScript MCP server tests
echo "[6/10] Running TypeScript MCP server tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-mcp.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript MCP tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript MCP tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 7. TypeScript skill manager tests
echo "[7/10] Running TypeScript skill manager tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-skills.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript skill tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript skill tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 8. TypeScript session manager tests
echo "[8/10] Running TypeScript session manager tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-sessions.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript session tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript session tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 9. TypeScript orchestration tests
echo "[9/10] Running TypeScript orchestration tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-orchestration.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "TypeScript orchestration tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "TypeScript orchestration tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 10. Full system integration tests
echo "[10/10] Running full system integration tests..."
echo "-----------------------------------"
RESULT=$(npx tsx backends/tests/test-full-integration.ts 2>&1 | tail -5)
echo "$RESULT"
if echo "$RESULT" | grep -q "0 failed"; then
    echo "Full integration tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    TOTAL_TS=$((TOTAL_TS+COUNT))
else
    echo "Full integration tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

echo "========================================"
echo "SUMMARY"
echo "========================================"
echo "Test suites passed: $PASS"
echo "Test suites failed: $FAIL"
echo "Total Python tests: $TOTAL_PYTHON"
echo "Total TypeScript tests: $TOTAL_TS"
echo "Total tests: $((TOTAL_PYTHON + TOTAL_TS))"
echo ""

if [ $FAIL -eq 0 ]; then
    echo "[SUCCESS] All test suites passed!"
    exit 0
else
    echo "[FAILURE] Some test suites failed"
    exit 1
fi
