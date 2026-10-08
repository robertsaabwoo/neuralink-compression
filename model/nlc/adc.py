"""ADC frame rule: what the compressor sees from the multiplexed slot stream (D5).

The ADC mux sends one sample per slot; `s_frame` marks slot 0. A frame is everything from one
`s_frame` to the next, whatever its length. Channel c of a frame is the sample in slot
slots[c] of that frame. Faults resolve as:

  short frame (s_frame early)   channels whose slot was not reached repeat their previous
                                sample; mid-scale 512 if the channel has none since enable
  long frame (s_frame late)     slots past the last selected one are ignored
  missing s_frame               two frames merge into one long frame: the second is ignored

So every frame yields exactly one sample per channel and the channel order never slips.
The hardware does not pad (decision D5, revised 2026-10-08): it aborts the packet that holds
a short frame and resumes at the next one; the padding here only keeps the frames-as-driven
count right for the packets after it, which do not depend on the short frame's values
(blocks and rANS states restart at every packet).
"""

from __future__ import annotations

import numpy as np

MID_SCALE = 512


def selected_stream(frames: list, slots: list[int], prev: list[int] | None = None) -> np.ndarray:
    """frames: one array of slot values per frame (any length). -> (n_frames, len(slots))."""
    last = list(prev) if prev is not None else [MID_SCALE] * len(slots)
    out = np.empty((len(frames), len(slots)), dtype=np.int64)
    for f, row in enumerate(frames):
        for c, s in enumerate(slots):
            if s < len(row):
                last[c] = int(row[s])
            out[f, c] = last[c]
    return out
