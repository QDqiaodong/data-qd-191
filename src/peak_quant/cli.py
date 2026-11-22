import argparse
import json
import sys
from .io import load,publish
from .release import release as run_release
from .release import release_show
from .request import load_and_parse
from .service import process

def _legacy(parser):
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)

def _release(parser):
    parser.add_argument('--store',required=True)
    parser.add_argument('--request',required=True)
    parser.add_argument('--source')

def _show(parser):
    parser.add_argument('--store',required=True)
    parser.add_argument('--batch',required=True)
    parser.add_argument('--revision',required=True,type=int)

def main(argv=None):
    parser=argparse.ArgumentParser(prog='peak-quant')
    sub=parser.add_subparsers(dest='command')
    _release(sub.add_parser('release'))
    _show(sub.add_parser('release-show'))
    # The v1 invocation remains the default flat form; a subparser-less call
    # keeps the historical required --source/--output contract.
    legacy=argparse.ArgumentParser(prog='peak-quant')
    _legacy(legacy)
    try:
        if argv is None:
            argv=sys.argv[1:]
        if argv and argv[0] in ('release','release-show'):
            args=parser.parse_args(argv)
        else:
            args=legacy.parse_args(argv)
            args.command=None
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        if args.command=='release':
            request=load_and_parse(args.request)
            payload,_revision,_retried=run_release(args.store,request,args.source)
            sys.stdout.buffer.write(payload)
            return 0
        if args.command=='release-show':
            payload=release_show(args.store,args.batch,args.revision)
            sys.stdout.buffer.write(payload)
            return 0
        result=process(load(args.source));publish(args.output,result)
    except (ValueError,OSError,UnicodeError,RecursionError,json.JSONDecodeError) as e:
        print(f'error: {e}',file=sys.stderr);return 2
    print(json.dumps(result['summary'],ensure_ascii=False,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
