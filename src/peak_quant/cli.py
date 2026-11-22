import argparse
import json
import sys
from .assemble import assemble
from .contrast import contrast as run_contrast
from .explain import build_package, load_inputs
from .io import load,publish
from .release import release as run_release
from .release import release_show
from .request import load_and_parse
from .service import process

def _legacy(parser):
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)

def _assemble(parser):
    parser.add_argument('--plan',required=True)
    parser.add_argument('--output',required=True)

def _release(parser):
    parser.add_argument('--store',required=True)
    parser.add_argument('--request',required=True)
    parser.add_argument('--source')

def _show(parser):
    parser.add_argument('--store',required=True)
    parser.add_argument('--batch',required=True)
    parser.add_argument('--revision',required=True,type=int)

def _explain(parser):
    parser.add_argument('--source',required=True)
    parser.add_argument('--candidates',required=True)
    parser.add_argument('--output',required=True)

def _contrast(parser):
    parser.add_argument('--package',required=True)
    parser.add_argument('--query',required=True)
    parser.add_argument('--output',required=True)

_SUBCOMMANDS = ('release','release-show','explain','contrast','assemble')

def main(argv=None):
    parser=argparse.ArgumentParser(prog='peak-quant')
    sub=parser.add_subparsers(dest='command')
    _release(sub.add_parser('release'))
    _show(sub.add_parser('release-show'))
    _explain(sub.add_parser('explain'))
    _contrast(sub.add_parser('contrast'))
    _assemble(sub.add_parser('assemble'))
    # The v1 invocation remains the default flat form; a subparser-less call
    # keeps the historical required --source/--output contract.
    legacy=argparse.ArgumentParser(prog='peak-quant')
    _legacy(legacy)
    try:
        if argv is None:
            argv=sys.argv[1:]
        if argv and argv[0] in _SUBCOMMANDS:
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
        if args.command=='explain':
            source_raw,candidates_raw=load_inputs(args.source,args.candidates)
            package=build_package(source_raw,candidates_raw)
            publish(args.output,package)
            sys.stdout.buffer.write((json.dumps(
                {'package_version':package['package_version'],
                 'runs':len(package['runs']),
                 'candidates':len(package['candidates'])},
                ensure_ascii=False,sort_keys=True)+'\n').encode('utf-8'))
            return 0
        if args.command=='contrast':
            report=run_contrast(args.package,args.query)
            publish(args.output,report)
            sys.stdout.buffer.write((json.dumps(
                {'run_id':report['run_id'],
                 'status':report['result']['status']},
                ensure_ascii=False,sort_keys=True)+'\n').encode('utf-8'))
            return 0
        if args.command=='assemble':
            try:
                assemble(args.plan,args.output)
            except MemoryError as e:
                # The assembler streams to disk but still honors a process
                # memory cap; treat an exhausted cap like any other failure.
                print(f'error: assemble resource error: {e}',file=sys.stderr)
                return 2
            sys.stdout.buffer.write((json.dumps(
                {'output':args.output},ensure_ascii=False,sort_keys=True)+'\n').encode('utf-8'))
            return 0
        result=process(load(args.source));publish(args.output,result)
    except (ValueError,OSError,UnicodeError,RecursionError,json.JSONDecodeError) as e:
        print(f'error: {e}',file=sys.stderr);return 2
    print(json.dumps(result['summary'],ensure_ascii=False,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
