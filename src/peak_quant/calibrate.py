from fractions import Fraction

def fit(points):
    if len(points)<2:raise ValueError('at least two standards required per analyte')
    n=len(points);sx=sum(x for _,x,_ in points);sy=sum(y for _,_,y in points)
    denominator=n*sum(x*x for _,x,_ in points)-sx*sx
    if not denominator:raise ValueError('standard concentrations must not all be equal')
    slope=(n*sum(x*y for _,x,y in points)-sx*sy)/denominator
    if slope<=0:raise ValueError('positive calibration slope required')
    intercept=(sy-slope*sx)/n
    residuals=[{'run_id':id,'residual':str(y-(slope*x+intercept))} for id,x,y in points]
    return {'slope':str(slope),'intercept':str(intercept),'min_concentration':str(min(x for _,x,_ in points)),
            'max_concentration':str(max(x for _,x,_ in points)),'residuals':residuals,'squared_residual_sum':str(sum(Fraction(r['residual'])**2 for r in residuals))}
