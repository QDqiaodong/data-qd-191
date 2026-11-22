from .checks import array,fields,ident,number,unique

def validate(data):
    fields(data,('analytes','runs'))
    analytes={}
    for a in array(data['analytes'],1,30):
        fields(a,('id','window'));ident(a['id'])
        lo,hi=[number(x) for x in array(a['window'],2,2)]
        if lo<0 or hi<=lo:raise ValueError('positive retention window required')
        if a['id'] in analytes:raise ValueError('duplicate analyte')
        analytes[a['id']]=(lo,hi)
    runs=array(data['runs'],1,200)
    for run in runs:
        fields(run,('id','kind','samples'),('concentrations',));ident(run['id'])
        if run['kind'] not in ('standard','unknown'):raise ValueError('unknown run kind')
        samples=array(run['samples'],2,2000);previous=None
        for row in samples:
            t,y=[number(x) for x in array(row,2,2)]
            if t<0 or (previous is not None and t<=previous):raise ValueError('sample times strictly increase')
            previous=t
        first,last=number(samples[0][0]),number(samples[-1][0])
        if any(lo<first or hi>last for lo,hi in analytes.values()):raise ValueError('every window must have full sample coverage')
        if run['kind']=='standard':
            concentrations=run.get('concentrations')
            if not isinstance(concentrations,dict) or set(concentrations)!=set(analytes):raise ValueError('standards require all analyte concentrations')
            if any(number(v)<0 for v in concentrations.values()):raise ValueError('negative standard concentration')
        elif 'concentrations' in run:raise ValueError('unknown run must not include known concentrations')
    unique([r['id'] for r in runs])
    return analytes,runs
