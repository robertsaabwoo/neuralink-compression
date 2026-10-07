import numpy as np
import pytest

from nlc.data import synthetic


@pytest.fixture(scope="session")
def neural():
    """8 channels x ~0.2 s of synthetic 10-bit data."""
    return synthetic(n_channels=8, n_samples=4096, seed=1)


@pytest.fixture
def rng():
    return np.random.default_rng(1234)
