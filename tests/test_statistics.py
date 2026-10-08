import numpy as np
from quran_analysis.statistics import holm, monte_carlo_p


def test_holm_arbitrary_dependence_example():
    assert np.allclose(holm([.01,.04,.03]), [.03,.06,.06])


def test_monte_carlo_plus_one_and_inclusive_ties():
    assert monte_carlo_p(10,[1,2,3]) == .25
    assert monte_carlo_p(2,[1,2,3]) == .75
    assert monte_carlo_p(2,[1,2,3],tail="less") == .75


def test_holm_does_not_depend_on_row_order():
    p=np.array([.003,.02,.8,.04])
    order=np.array([2,0,3,1])
    assert np.allclose(holm(p)[order],holm(p[order]))
