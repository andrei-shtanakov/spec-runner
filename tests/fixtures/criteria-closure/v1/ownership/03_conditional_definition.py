import sys

if sys.platform:
    def test_a():
        '''ENC:BEH-01'''

try:
    import json
except ImportError:  # ENC:BEH-02
    pass
else:
    def test_b():
        '''ENC:BEH-03'''
