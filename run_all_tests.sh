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
echo "[1/4] Running Python backend module tests..."
echo "-----------------------------------"
RESULT=$(python3 -m unittest tests.test_backends 2>&1)
if echo "$RESULT" | grep -q "^OK$"; then
    echo "Python module tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "Ran [0-9]+ tests" | grep -oE "[0-9]+")
    TOTAL_PYTHON=$((TOTAL_PYTHON+COUNT))
    echo "  ($COUNT tests passed)"
else
    echo "Python module tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 2. Python E2E tests
echo "[2/4] Running Python E2E tests..."
echo "-----------------------------------"
RESULT=$(python3 -m unittest tests.test_e2e_backend 2>&1)
if echo "$RESULT" | grep -q "^OK$"; then
    echo "Python E2E tests: PASS"
    PASS=$((PASS+1))
    COUNT=$(echo "$RESULT" | grep -oE "Ran [0-9]+ tests" | grep -oE "[0-9]+")
    TOTAL_PYTHON=$((TOTAL_PYTHON+COUNT))
    echo "  ($COUNT tests passed)"
else
    echo "Python E2E tests: FAIL"
    FAIL=$((FAIL+1))
fi
echo ""

# 3. TypeScript backend loader tests
echo "[3/5] Running TypeScript backend loader tests..."
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

# 4. TypeScript harness tests
echo "[4/5] Running TypeScript harness tests..."
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

# 5. TypeScript integration tests
echo "[5/5] Running TypeScript integration tests..."
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
