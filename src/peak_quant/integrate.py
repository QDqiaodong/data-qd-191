from bisect import bisect_right
from fractions import Fraction
from .checks import number

def integrate(samples,lo,hi):
    points=[(number(t),number(y)) for t,y in samples]
    return integrate_points(points,lo,hi)

def integrate_points(points,lo,hi):
    """Integrate pre-parsed (t,y) Fraction points with the v1 fixed rules."""
    times=[t for t,y in points]
    start,end=_interpolate(times,points,lo),_interpolate(times,points,hi)
    baseline=lambda t:start+(end-start)*(t-lo)/(hi-lo)
    clipped=[(lo,start)]+[(t,y) for t,y in points if lo<t<hi]+[(hi,end)]
    segments=[]
    for (a,ya),(b,yb) in zip(clipped,clipped[1:]):
        area=((ya-baseline(a))+(yb-baseline(b)))*(b-a)/2
        segments.append({'start':str(a),'end':str(b),'area':str(area)})
    corrected=[(y-baseline(t),t) for t,y in clipped]
    height=max(y for y,t in corrected)
    apex=min(t for y,t in corrected if y==height)
    return {'area':str(sum(Fraction(s['area']) for s in segments)),'baseline_start':str(start),'baseline_end':str(end),
            'apex_time':str(apex),'height_above_baseline':str(height),'segments':segments}

def _interpolate(times,points,t):
    if t==times[-1]:return points[-1][1]
    i=bisect_right(times,t)-1
    a,ya=points[i];b,yb=points[i+1]
    return ya+(yb-ya)*(t-a)/(b-a)

def area_of(points,lo,hi,start=None,end=None):
    """Signed baseline-corrected area only, identical math to integrate_points.

    The baseline is linear, so its trapezoid integral over [lo, hi] is simply
    (start+end)/2*(hi-lo); subtracting it from the raw trapezoid integral of
    the clipped signal gives the same exact Fraction as the segment sum.
    Window endpoints may be pre-interpolated by the caller (they are
    fold-independent) to avoid repeating the same work for every fold.
    """
    times=[t for t,y in points]
    if start is None:start=_interpolate(times,points,lo)
    if end is None:end=_interpolate(times,points,hi)
    clipped=[(lo,start)]+[(t,y) for t,y in points if lo<t<hi]+[(hi,end)]
    signal_sum=Fraction(0)
    for (a,ya),(b,yb) in zip(clipped,clipped[1:]):
        signal_sum+=(ya+yb)*(b-a)/2
    return signal_sum-(start+end)*(hi-lo)/2
