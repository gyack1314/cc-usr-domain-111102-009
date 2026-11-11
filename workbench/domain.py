import sqlite3
from fractions import Fraction
from .io import read_json
TABLES={'donations':['id','day','region','donor','amount'],'expenses':['id','day','region','amount'],'donation_projects':['fact_id','project','numerator','denominator'],'expense_projects':['fact_id','project','numerator','denominator']}
def load_database(path,directory):
    from pathlib import Path
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(path) as c:
        for name,fields in TABLES.items():
            integer={'amount','numerator','denominator'}
            columns=','.join('"'+f+'" '+('INTEGER' if f in integer else 'TEXT') for f in fields)
            c.execute('CREATE TABLE IF NOT EXISTS "'+name+'" ('+columns+')')
            if c.execute('SELECT count(*) FROM "'+name+'"').fetchone()[0]:raise ValueError('资料表不为空')
            values=read_json(Path(directory)/(name+'.json'))
            c.executemany('INSERT INTO "'+name+'" VALUES('+','.join('?' for _ in fields)+')',([r[f] for f in fields] for r in values))
def weight(row):
    n=row['numerator'];d=row['denominator']
    if type(n) is not int or type(d) is not int or n<0 or d<=0:raise ValueError('权重无效')
    return Fraction(n,d)
def raw_total(path,table):
    if table not in ['donations','expenses']:raise ValueError('事实表未登记')
    with sqlite3.connect(path) as c:return c.execute('SELECT coalesce(sum(amount),0) FROM "'+table+'"').fetchone()[0]
