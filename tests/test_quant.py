import json
from pathlib import Path
import unittest
from fractions import Fraction
from peak_quant.checks import number
from peak_quant.integrate import area_of, integrate, integrate_points
from peak_quant.service import process

def sample():return json.loads((Path(__file__).resolve().parents[1]/'fixtures/input.json').read_text())

class QuantTests(unittest.TestCase):
    def test_integration_calibration_and_unknown(self):
        r=process(sample())
        self.assertEqual(r['calibrations']['A']['slope'],'2')
        self.assertEqual(r['calibrations']['A']['intercept'],'0')
        self.assertEqual(r['quantified'][0]['concentration'],'1')
        self.assertEqual(r['integrations']['u1']['A']['area'],'2')
        self.assertEqual(r['summary'],dict(runs=3,analytes=1,quantified=1,extrapolated=0))

    def test_fractional_window_and_sloped_baseline(self):
        r=integrate([['0','0'],['1','4'],['2','2']],Fraction(1,2),Fraction(3,2))
        self.assertEqual(r['area'],'3/4')
        self.assertEqual(r['baseline_start'],'2');self.assertEqual(r['baseline_end'],'3')
        self.assertEqual(r['apex_time'],'1')

    def test_fast_area_matches_segment_integration_exactly(self):
        # The benchmark fast path must reproduce the v1 segment sum exactly,
        # including endpoint interpolation, baseline subtraction and signs.
        samples=[['0','1'],['0.5','4'],['1','-2'],['1.5','5'],['2','2']]
        points=[(number(t),number(y)) for t,y in samples]
        for lo,hi in [(Fraction(0),Fraction(2)),(Fraction(1,4),Fraction(7,4)),
                      (Fraction(1,2),Fraction(2)),(Fraction(0),Fraction(1))]:
            full=integrate(samples,lo,hi)
            self.assertEqual(Fraction(full['area']),area_of(points,lo,hi))
            # And via pre-parsed points with shared endpoint interpolation.
            self.assertEqual(Fraction(integrate_points(points,lo,hi)['area']),
                             area_of(points,lo,hi))

    def test_negative_integral_is_not_clamped(self):
        d=sample();d['runs'][2]['samples'][1][1]='1'
        q=process(d)['quantified'][0]
        self.assertEqual(q['concentration'],'-1');self.assertTrue(q['negative']);self.assertTrue(q['extrapolated'])

    def test_replicates_fit_with_residuals(self):
        d=sample();d['runs'].insert(2,dict(id='replicate',kind='standard',concentrations={'A':'2'},samples=[['0','0'],['1','6'],['2','0']]))
        c=process(d)['calibrations']['A']
        self.assertEqual(c['slope'],'5/2');self.assertEqual(c['squared_residual_sum'],'2')

    def test_missing_coverage_equal_concentrations_and_flat_response(self):
        d=sample();d['analytes'][0]['window'][0]='-1'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['runs'][1]['concentrations']['A']='0'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['runs'][1]['samples'][1][1]='2'
        with self.assertRaises(ValueError):process(d)

    def test_time_identity_and_schema_rejected(self):
        d=sample();d['runs'][0]['samples'][1][0]='0'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['runs'][1]['id']='s0'
        with self.assertRaises(ValueError):process(d)
        d=sample();d['runs'][2]['concentrations']={'A':'1'}
        with self.assertRaises(ValueError):process(d)

if __name__=='__main__':unittest.main()
