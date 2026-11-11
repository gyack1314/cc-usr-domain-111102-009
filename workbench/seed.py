import argparse
from .domain import load_database
if __name__=='__main__':
    p=argparse.ArgumentParser(description='装载合成事实');p.add_argument('database');p.add_argument('--samples',default='samples');a=p.parse_args();load_database(a.database,a.samples);print('已装载合成事实')
