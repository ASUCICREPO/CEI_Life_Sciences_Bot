import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { spawnSync } from 'child_process';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';

const BACKEND_DIR = path.join(__dirname, '..', '..');
const LAYER_DIR = path.join(BACKEND_DIR, 'lambdas', 'layers', 'data-source-deps');

/** [source relative to backend/, destination relative to the layer's python/] */
const STAGED_FILES: ReadonlyArray<[string, string]> = [
  ['agents/shared/config.py', 'shared/config.py'],
  ['agents/shared/schemas/citation.py', 'shared/schemas/citation.py'],
  ['agents/shared/tools/base.py', 'shared/tools/base.py'],
  ['agents/shared/tools/retrying_http.py', 'shared/tools/retrying_http.py'],
  ['lambdas/ncbi/limiter.py', 'ncbi_limiter.py'],
  ['lambdas/shared/data_source_runtime.py', 'data_source_runtime.py'],
];
const DATA_SOURCES_DIR = 'agents/shared/tools/data_sources';

// ADR: empty package __init__.py files | Rationale: the real shared/, shared/schemas/, shared/tools/ and
// shared/tools/data_sources/ __init__ modules import langgraph, the Code Interpreter tool, every schema and
// build ALL_DATA_SOURCE_TOOLS; a source Lambda needs none of that | Alternative: shipping the full agent
// package rejected — pulls langchain/langgraph into every Lambda
const EMPTY_PACKAGES = ['shared', 'shared/schemas', 'shared/tools', 'shared/tools/data_sources'];

const PIP_ARGS = [
  '-m', 'pip', 'install', '-r', 'requirements.txt',
  '--platform', 'manylinux2014_x86_64',
  '--implementation', 'cp',
  '--python-version', '3.14',
  '--only-binary=:all:',
];

/** Every data-source module file name (`pubmed.py`, ...), excluding the package __init__. */
function dataSourceModules(): string[] {
  return fs
    .readdirSync(path.join(BACKEND_DIR, DATA_SOURCES_DIR))
    .filter((name) => name.endsWith('.py') && name !== '__init__.py')
    .sort();
}

function allStagedFiles(): Array<[string, string]> {
  return [
    ...STAGED_FILES,
    ...dataSourceModules().map((name): [string, string] => [
      `${DATA_SOURCES_DIR}/${name}`,
      `shared/tools/data_sources/${name}`,
    ]),
  ];
}

/** Stage the layer's shared code under `pythonDir` (pip output already there). */
export function stageDataSourceCode(pythonDir: string): void {
  for (const pkg of EMPTY_PACKAGES) {
    fs.mkdirSync(path.join(pythonDir, pkg), { recursive: true });
    fs.writeFileSync(path.join(pythonDir, pkg, '__init__.py'), '');
  }
  for (const [from, to] of allStagedFiles()) {
    fs.copyFileSync(path.join(BACKEND_DIR, from), path.join(pythonDir, to));
  }
}

class LocalPythonBundling implements cdk.ILocalBundling {
  tryBundle(outputDir: string): boolean {
    const pythonDir = path.join(outputDir, 'python');
    fs.mkdirSync(pythonDir, { recursive: true });
    // ADR: try `python3` then `python` | Rationale: Linux CodeBuild images ship `python3`; Windows dev hosts ship `python`
    for (const python of ['python3', 'python']) {
      const result = spawnSync(python, [...PIP_ARGS, '-t', pythonDir], { cwd: LAYER_DIR, stdio: ['ignore', 2, 2] });
      if (result.status === 0) {
        stageDataSourceCode(pythonDir);
        return true;
      }
    }
    return false;
  }
}

function sourceHash(): string {
  const hash = crypto.createHash('sha256');
  const files: Array<[string, string]> = [['lambdas/layers/data-source-deps/requirements.txt', ''], ...allStagedFiles()];
  for (const [from, to] of files) {
    hash.update(from).update(to).update(fs.readFileSync(path.join(BACKEND_DIR, from)));
  }
  for (const pkg of EMPTY_PACKAGES) hash.update(`empty:${pkg}`);
  return hash.digest('hex');
}

/**
 * Lambda layer for the F4 data-source Gateway targets: pinned pydantic + requests, the ten source tools,
 * their base class and retry helper, and `data_source_runtime`. Built during `cdk synth`/`cdk deploy`
 * (CodeBuild, B91); the Docker image is only a fallback.
 */
export class DataSourceLayer extends Construct {
  public readonly layer: lambda.LayerVersion;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // ADR: skip bundling under Jest | Rationale: same as PythonDepsLayer — unit tests must not run pip or Docker
    const underJest = process.env.JEST_WORKER_ID !== undefined;

    const dockerCopy = [
      ...EMPTY_PACKAGES.map((pkg) => `mkdir -p /asset-output/python/${pkg} && : > /asset-output/python/${pkg}/__init__.py`),
      ...allStagedFiles().map(([from, to]) => `cp /src/${from} /asset-output/python/${to}`),
    ];

    const code = underJest
      ? lambda.Code.fromAsset(LAYER_DIR)
      : lambda.Code.fromAsset(LAYER_DIR, {
        // ADR: custom asset hash | Rationale: the asset dir holds only requirements.txt, so the default hash would miss edits to the staged source code
        assetHashType: cdk.AssetHashType.CUSTOM,
        assetHash: sourceHash(),
        bundling: {
          image: lambda.Runtime.PYTHON_3_14.bundlingImage,
          local: new LocalPythonBundling(),
          volumes: [
            { hostPath: path.join(BACKEND_DIR, 'agents'), containerPath: '/src/agents' },
            { hostPath: path.join(BACKEND_DIR, 'lambdas'), containerPath: '/src/lambdas' },
          ],
          command: [
            'bash', '-c',
            [
              'pip install -r requirements.txt --platform manylinux2014_x86_64 --implementation cp --python-version 3.14 --only-binary=:all: -t /asset-output/python',
              ...dockerCopy,
              'find /asset-output/python -name __pycache__ -prune -exec rm -rf {} +',
            ].join(' && '),
          ],
        },
      });

    this.layer = new lambda.LayerVersion(this, 'Layer', {
      code,
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_14],
      compatibleArchitectures: [lambda.Architecture.X86_64],
      description: 'pydantic + requests, the F4 data-source tools, and data_source_runtime',
    });
  }
}
