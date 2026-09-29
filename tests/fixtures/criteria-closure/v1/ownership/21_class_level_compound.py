import contextlib


class TestK:
    if True:  # ENC:BEH-01
        def test_a(self):
            '''ENC:BEH-02'''
    else:
        LABEL = 'ENC:BEH-03'


with contextlib.suppress(Exception):  # ENC:BEH-04
    def test_w():
        '''ENC:BEH-05'''

for _ in range(1):  # ENC:BEH-06
    def test_f():
        '''ENC:BEH-07'''
