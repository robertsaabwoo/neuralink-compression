"""ADC frame rule (model/nlc/adc.py, decision D5)."""

import numpy as np

from nlc.adc import MID_SCALE, selected_stream


def test_regular_frames_pick_the_selected_slots():
    rows = [np.arange(256) + 1000 * f for f in range(3)]
    y = selected_stream(rows, [0, 7, 255])
    assert y.tolist() == [[1000 * f, 1000 * f + 7, 1000 * f + 255] for f in range(3)]


def test_short_frame_repeats_previous_sample():
    rows = [np.arange(256), np.arange(100) + 5000, np.arange(256) + 9000]
    y = selected_stream(rows, [3, 150])
    assert y[1].tolist() == [5003, 150]          # slot 150 not reached: previous value
    assert y[2].tolist() == [9003, 9150]


def test_short_first_frame_uses_mid_scale():
    y = selected_stream([np.arange(10)], [3, 200])
    assert y[0].tolist() == [3, MID_SCALE]


def test_long_frame_ignores_extra_slots():
    long = np.concatenate([np.arange(256), np.arange(256) + 7000])   # missing s_frame
    y = selected_stream([long], [5, 255])
    assert y[0].tolist() == [5, 255]
