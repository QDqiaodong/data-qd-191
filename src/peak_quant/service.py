from fractions import Fraction
from .calibrate import fit
from .checks import number
from .integrate import integrate
from .model import validate

def process(data):
    analytes,runs=validate(data)
    results={r['id']:{a:integrate(r['samples'],lo,hi) for a,(lo,hi) in sorted(analytes.items())} for r in runs}
    calibrations={a:fit([(r['id'],number(r['concentrations'][a]),Fraction(results[r['id']][a]['area'])) for r in runs if r['kind']=='standard']) for a in sorted(analytes)}
    quantified=[]
    for run in runs:
        if run['kind']=='unknown':
            for a,cal in calibrations.items():
                value=(Fraction(results[run['id']][a]['area'])-Fraction(cal['intercept']))/Fraction(cal['slope'])
                quantified.append({'run_id':run['id'],'analyte_id':a,'concentration':str(value),'negative':value<0,
                                   'extrapolated':not Fraction(cal['min_concentration'])<=value<=Fraction(cal['max_concentration'])})
    return {'schema_version':1,'integrations':results,'calibrations':calibrations,'quantified':quantified,
            'summary':{'runs':len(runs),'analytes':len(analytes),'quantified':len(quantified),'extrapolated':sum(q['extrapolated'] for q in quantified)}}
