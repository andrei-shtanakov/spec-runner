try:
    import json
except ImportError:
    def test_in_except():
        '''ENC:BEH-01'''
finally:
    def test_in_finally():
        '''ENC:BEH-02'''

match 1:
    case 1:
        def test_in_case():
            '''ENC:BEH-03'''
