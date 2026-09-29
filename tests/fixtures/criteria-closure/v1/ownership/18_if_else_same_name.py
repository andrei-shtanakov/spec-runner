import sys

if sys.platform == 'win32':
    def test_a():
        '''ENC:BEH-01'''
else:
    def test_a():
        '''ENC:BEH-02'''
