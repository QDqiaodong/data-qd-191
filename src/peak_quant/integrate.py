from bisect import bisect_right
from .checks import number

def integrate(samples,lo,hi):
    points=[(number(t),number(y)) for t,y in samples]
    times=[t for t,y in points]
    def at(t):
        if t==times[-1]:return points[-1][1]
        i=bisect_right(times,t)-1
        a,ya=points[i];b,yb=points[i+1]
        return ya+(yb-ya)*(t-a)/(b-a)
    start,end=at(lo),at(hi)
    baseline=lambda t:start+(end-start)*(t-lo)/(hi-lo)
    clipped=[(lo,start)]+[(t,y) for t,y in points if lo<t<hi]+[(hi,end)]
    segments=[]
    for (a,ya),(b,yb) in zip(clipped,clipped[1:]):
        area=((ya-baseline(a))+(yb-baseline(b)))*(b-a)/2
        segments.append({'start':str(a),'end':str(b),'area':str(area)})
    corrected=[(y-baseline(t),t) for t,y in clipped]
    height=max(y for y,t in corrected)
    apex=min(t for y,t in corrected if y==height)
    return {'area':str(sum(__import__('fractions').Fraction(s['area']) for s in segments)),'baseline_start':str(start),'baseline_end':str(end),
            'apex_time':str(apex),'height_above_baseline':str(height),'segments':segments}
