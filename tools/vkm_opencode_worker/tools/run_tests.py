#!/usr/bin/env python3
"""Offline suite with inspectable JSON results. Never calls a model/API.

On Windows this exercises the process supervisor on Windows, but still uses a
fake OpenCode for integration tests. It is NOT native OpenCode/MCP acceptance.
"""
from __future__ import annotations
import argparse
import io
import json
import os
from pathlib import Path
import platform
import sys
import time
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]

class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []; self.started = {}
    def startTest(self, test):
        self.started[test.id()] = time.monotonic(); super().startTest(test)
    def record(self, test, status, detail=None):
        row = {"id":test.id(), "status":status, "duration_sec":round(time.monotonic()-self.started.get(test.id(),time.monotonic()),6)}
        if detail is not None: row["detail"] = detail
        self.records.append(row)
    def addSuccess(self, test):
        self.record(test,"PASS"); super().addSuccess(test)
    def addFailure(self, test, err):
        self.record(test,"FAIL",self._exc_info_to_string(err,test)); super().addFailure(test,err)
    def addError(self, test, err):
        self.record(test,"ERROR",self._exc_info_to_string(err,test)); super().addError(test,err)
    def addSkip(self, test, reason):
        self.record(test,"SKIP",reason); super().addSkip(test,reason)
    def addExpectedFailure(self,test,err):
        self.record(test,"EXPECTED_FAILURE");super().addExpectedFailure(test,err)
    def addUnexpectedSuccess(self,test):
        self.record(test,"UNEXPECTED_SUCCESS");super().addUnexpectedSuccess(test)
    def addSubTest(self, test, subtest, err):
        if err is not None:self.record(subtest,"SUBTEST_ERROR",self._exc_info_to_string(err,test))
        super().addSubTest(test,subtest,err)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    log=a.output.with_suffix('.log')
    if a.output.exists() or log.exists():p.error('Output already exists; preserve previous receipts and select a new path.')
    out=io.StringIO(); started=time.monotonic()
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    result=unittest.TextTestRunner(stream=out,verbosity=2,resultclass=RecordingResult).run(suite)
    report={"schema":"vkm.offline.test-results/2","created_at":datetime.now(timezone.utc).isoformat(),
            "python":platform.python_version(),"platform":sys.platform,"os_name":os.name,
            "scope":"stdlib contracts; temporary real Git; fake OpenCode; local process supervision",
            "model_calls":0,"real_opencode_calls":0,"native_mcp_calls":0,
            "native_opencode_acceptance":"NOT_RUN","windows_job_object":"EXERCISED_BY_LOCAL_PROCESS_TESTS" if os.name=='nt' else "NOT_RUN",
            "tests_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
            "skipped":len(result.skipped),"successful":result.wasSuccessful(),
            "duration_sec":round(time.monotonic()-started,3),"tests":result.records}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    with a.output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2);f.write('\n')
    with log.open('x',encoding='utf-8') as f:f.write(out.getvalue())
    print(json.dumps({k:v for k,v in report.items() if k!='tests'},ensure_ascii=False,indent=2))
    return 0 if result.wasSuccessful() else 1

if __name__=='__main__':raise SystemExit(main())
