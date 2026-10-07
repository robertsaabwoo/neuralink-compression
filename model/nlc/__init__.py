"""nlc: bit-exact Python golden model for the multi-mode neural compression engine."""

from .bitstream import BitReader, BitWriter
from .lossless import LosslessCodec, RiceConfig
from .lossy import LossyCodec, LossyConfig, train_tables
from .packet import Mode, decode_stream, encode_stream, stream_bits
from .spikes import (BinnedConfig, BinnedSpikeCodec, FrontEndConfig, SbpConfig,
                     SpikeBandPowerCodec, calibrate_thresholds)

__all__ = [
    "BitReader", "BitWriter", "LosslessCodec", "RiceConfig", "LossyCodec", "LossyConfig",
    "train_tables", "Mode", "encode_stream", "decode_stream", "stream_bits",
    "BinnedConfig", "BinnedSpikeCodec", "FrontEndConfig", "SbpConfig",
    "SpikeBandPowerCodec", "calibrate_thresholds",
]
