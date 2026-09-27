# demo-repo

Tiny demo repository for FixFork: one module, one planted bug, one failing
test. Used by the offline demo (`--fake`) and by the test suite.

Bug (on purpose): a discount is applied in the wrong direction, so a
discounted order costs *more* than the undiscounted one.

Run the failing test:

```bash
python3 -m unittest discover -s tests -v
```
