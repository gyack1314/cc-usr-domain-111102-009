import tempfile
from pathlib import Path
from fractions import Fraction
from workbench.domain import load_database,weight,raw_total
def check(c,root,read):
    c.assertEqual(weight({'numerator':1,'denominator':3}),Fraction(1,3))
    with c.assertRaises(ValueError):weight({'numerator':1,'denominator':0})
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'facts.sqlite3';load_database(p,root/'samples')
        c.assertEqual(raw_total(p,'donations'),601);c.assertEqual(raw_total(p,'expenses'),190)
        with c.assertRaises(ValueError):load_database(p,root/'samples')
        with c.assertRaises(ValueError):raw_total(p,'x;drop table donations')
