# gmt_fast — optional Rust acceleration

**You do not need this.** Nothing in the harness is currently CPU-bound
enough to justify it. It exists so that when the camera path becomes the
bottleneck, the first Rust project is already scoped, written, and
verifiable against the Python it replaces.

## Build it

```bash
# 1. install rust (once)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# 2. install maturin -- it builds rust into an importable python module
pip install maturin

# 3. build and install into the current python environment
cd rust/gmt_fast
maturin develop --release        # --release matters: debug builds are ~30x slower
```

## Verify it against Python before you trust it

```python
import gmt_fast, statistics

profile = [10.0] * 250 + [40.0] + [10.0] * 5
print(gmt_fast.mad_peak(profile))     # -> (large score, index 250)
```

Then run the harness tests. `t_frame_gate.py` keeps its NumPy path; the
Rust one is opt-in, so you can A/B them on the same frames and confirm they
agree before switching.

## Wire it in

In `gmt/tasks/t_frame_gate.py`:

```python
try:
    import gmt_fast
    _FAST = True
except ImportError:
    _FAST = False

def _mad_peak(profile):
    if _FAST:
        return gmt_fast.mad_peak([float(x) for x in profile])
    ...existing numpy path...
```

Same pattern as every other optional dependency in this repo: if it is
there, use it; if not, carry on.
