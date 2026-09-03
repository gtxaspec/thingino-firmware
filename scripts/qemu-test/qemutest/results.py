"""Check bookkeeping and the ordered result list."""

import json


class TestResult:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.xfailed = []
        self.skipped = []
        self.results = []

    def ok(self, name, detail=""):
        self.passed.append(name)
        self.results.append({"name": name, "pass": True, "detail": detail})
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))

    def fail(self, name, detail=""):
        self.failed.append(name)
        self.results.append({"name": name, "pass": False, "detail": detail})
        print(f"  FAIL  {name}" + (f"  ({detail})" if detail else ""))

    def check(self, name, condition, detail=""):
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)

    def xfail(self, name, condition, reason=""):
        """Known-gap test: failing is expected and doesn't fail the run,
        passing is celebrated."""
        if condition:
            self.passed.append(name)
            self.results.append({"name": name, "pass": True,
                                 "detail": f"XPASS: {reason}", "xfail": True})
            print(f"  XPASS {name}  ({reason})")
        else:
            self.xfailed.append(name)
            self.results.append({"name": name, "pass": False,
                                 "detail": f"expected: {reason}", "xfail": True})
            print(f"  XFAIL {name}  ({reason})")

    def skip(self, name, reason=""):
        self.skipped.append(name)
        self.results.append({"name": name, "pass": True, "skip": True,
                             "detail": f"skipped: {reason}"})
        print(f"  SKIP  {name}  ({reason})")

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 50}")
        print(f"Results: {len(self.passed)}/{total} passed"
              + (f", {len(self.xfailed)} xfail" if self.xfailed else "")
              + (f", {len(self.skipped)} skipped" if self.skipped else ""))
        if self.failed:
            print(f"Failed:  {', '.join(self.failed)}")
        return len(self.failed) == 0

    def save_json(self, path):
        with open(path, "w") as f:
            json.dump(self.results, f, indent=2)
        print(f"Results saved to {path}")
