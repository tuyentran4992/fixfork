# FixFork run report

- Repo: `/tmp/nuitka-fas/Nuitka`
- Test command: `sh /data/hackathon/nghien-cuu/repo-7-nuitka-4063/test-nuitka-cach-ly.sh`
- Baseline: **FAILED (1 of 0)**

## Branches

| Branch | Status | Tests | Lines changed | Tokens | Cost ($) |
|---|---|---|---|---|---|
| 1 | green | OK (0 tests) | 2 | 0 | 0.00000 |
| 2 | red | FAILED (1 of 0) | 11 | 39102 | 0.00292 |

## Verdict

- Winner: **branch 1**
- Why: branch 1 passed the tests (OK (0 tests)) with 2 line(s) changed
- Tokens: **91587** total (52485 diagnosis) · cost ~ **$0.0245**

## Hypotheses (raced)

- **1. Fix error message format for list.index in Python>=0x300** — The test expects the error message 'list.index(x): x not in list' for ValueError when list.index fails to find the item, but Nuitka produces '9 is not in list' (the old format). The function _LIST_INDEX_COMMON in HelpersLists.c sets the error message for Python>=0x300 using PyErr_Format with '%R is not in list', which includes the item. This must be changed to the fixed string 'list.index(x): x not in list' without formatting. _(files: nuitka/build/static_src/HelpersLists.c)_
- **2. Use PyUnicode_FromString for list.index error in Python>=0x300** — The error message for list.index in Python>=0x300 should be a fixed string without item formatting. Instead of PyErr_Format, we can use PyUnicode_FromString to create the error string and SET_CURRENT_EXCEPTION_TYPE0_VALUE1 to set the exception, mirroring the approach used for Python<0x300. This ensures consistent error handling across Python versions. _(files: nuitka/build/static_src/HelpersLists.c)_

## Web research (Tavily)

- Query: `ValueError message cu (pre-3.14) cho list.index - khac CPython 3.14. sh /data/hackathon/nghien-cuu/repo-7-nuitka-4063/test-nuitka-cach-ly.sh`
- Source: https://nuitka.net/changelog/Changelog.html
- Source: https://github.com/Nuitka/Nuitka/issues/3766
- Source: https://nuitka.net/posts/nuitka-release-41.html
- Source: https://nuitka.net/posts/nuitka-release-28.html
- Source: https://discuss.python.org/t/python-3-14-6-and-3-13-14-are-now-available/107714

## Winning diff

```diff
diff --git a/nuitka/build/static_src/HelpersLists.c b/nuitka/build/static_src/HelpersLists.c
--- a/nuitka/build/static_src/HelpersLists.c
+++ b/nuitka/build/static_src/HelpersLists.c
@@ -641,7 +641,7 @@
     SET_CURRENT_EXCEPTION_TYPE0_VALUE1(tstate, PyExc_ValueError, err_string);
     return NULL;
 #else
-    PyErr_Format(PyExc_ValueError, "%R is not in list", item);
+    PyErr_SetString(PyExc_ValueError, "list.index(x): x not in list");
     return NULL;
 #endif
 }
```

## Notes

- web research (Tavily): 5 source(s) for query: ValueError message cu (pre-3.14) cho list.index - khac CPython 3.14. sh /data/hackathon/nghien-cuu/repo-7-nuitka-4063/test-nuitka-cach-ly.sh
- machine scan unavailable (no usable signature in the log, or no occurrence found in log-referenced files); those files get the larger fallback budget in the prompt
- reply failed to parse as a whole; salvaged 2 of 4 top-level object(s) from the raw text
- reply carried 2 of 3 requested hypotheses: kept the complete one(s) instead of discarding over the count (later drops and the branch cap still apply)
- diagnosis model produced 2 unique hypothesis(es) (52485 tokens, $0.0216)
