from quran_analysis.extension import equal_pair_count


def test_equivalence_pairs_against_manual_list():
    # a,b,c have frequency 1; d,e frequency 2: ab/ac/bc/de.
    assert equal_pair_count([1,1,1,2,2,3])==4
    assert equal_pair_count([(1,1),(1,1),(1,2),(2,2)])==1
