import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import { spawnSync } from 'child_process';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as lambda from 'aws-cdk-lib/aws-lambda';

const BACKEND_DIR = path.join(__dirname, '..', '..');
const LAYER_DIR = path.join(BACKEND_DIR, 'lambdas', 'layers', 'python-deps');
const PERSISTENCE_DIR = path.join(BACKEND_DIR, 'agents', 'shared', 'persistence');
const CONFIG_FILE = path.join(BACKEND_DIR, 'agents', 'shared', 'config.py');
const IDENTITY_FILE = path.join(BACKEND_DIR, 'agents', 'shared', 'runtime', 'identity.py');
// F13: POST /ui-actions validates the typed ActionRequest with the same model the runtimes use (pydantic only)
const CEI_UI_SCHEMA_FILE = path.join(BACKEND_DIR, 'agents', 'shared', 'schemas', 'cei_ui.py');
const MODEL_POLICY_FILE = path.join(BACKEND_DIR, 'agents', 'shared', 'runtime', 'model_policy.py');
const MODEL_DISCOVERY_FILE = path.join(BACKEND_DIR, 'agents', 'shared', 'runtime', 'model_discovery.py');
const HTTP_ERRORS_FILE = path.join(BACKEND_DIR, 'lambdas', 'shared', 'http_errors.py');

const PIP_ARGS = [
  '-m', 'pip', 'install', '-r', 'requirements.txt',
  '--platform', 'manylinux2014_x86_64',
  '--implementation', 'cp',
  '--python-version', '3.14',
  '--only-binary=:all:',
];

function copyPythonTree(from: string, to: string): void {
  fs.cpSync(from, to, {
    recursive: true,
    filter: (src) => !/(__pycache__|\.pyc$|\.pytest_cache)/.test(src),
  });
}

/** Stages the layer's `python/` tree: pip deps plus the shared persistence code and http_errors.py. */
function stageShared(pythonDir: string): void {
  const sharedDir = path.join(pythonDir, 'shared');
  fs.mkdirSync(sharedDir, { recursive: true });
  // ADR: minimal shared/__init__.py | Rationale: the real one lazily exposes state.py (langgraph deps); the layer carries only persistence/ and config.py
  fs.writeFileSync(path.join(sharedDir, '__init__.py'), '');
  fs.copyFileSync(CONFIG_FILE, path.join(sharedDir, 'config.py'));
  fs.mkdirSync(path.join(sharedDir, 'runtime'), { recursive: true });
  fs.writeFileSync(path.join(sharedDir, 'runtime', '__init__.py'), '');
  fs.copyFileSync(IDENTITY_FILE, path.join(sharedDir, 'runtime', 'identity.py'));
  // ADR: empty shared/schemas/__init__.py | Rationale: the real one imports every schema module; the layer needs only cei_ui.py
  fs.mkdirSync(path.join(sharedDir, 'schemas'), { recursive: true });
  fs.writeFileSync(path.join(sharedDir, 'schemas', '__init__.py'), '');
  fs.copyFileSync(CEI_UI_SCHEMA_FILE, path.join(sharedDir, 'schemas', 'cei_ui.py'));
  // F8: model_policy (blocklist/select_model) and model_discovery (ListFoundationModels/ListInferenceProfiles
  // caching) for models-settings-handler, session-metadata-handler, and session-router.
  fs.copyFileSync(MODEL_POLICY_FILE, path.join(sharedDir, 'runtime', 'model_policy.py'));
  fs.copyFileSync(MODEL_DISCOVERY_FILE, path.join(sharedDir, 'runtime', 'model_discovery.py'));
  copyPythonTree(PERSISTENCE_DIR, path.join(sharedDir, 'persistence'));
  fs.copyFileSync(HTTP_ERRORS_FILE, path.join(pythonDir, 'http_errors.py'));
}

class LocalPythonBundling implements cdk.ILocalBundling {
  tryBundle(outputDir: string): boolean {
    const pythonDir = path.join(outputDir, 'python');
    fs.mkdirSync(pythonDir, { recursive: true });
    // ADR: try `python3` then `python` | Rationale: Linux CodeBuild images ship `python3`; Windows dev hosts ship `python`
    for (const python of ['python3', 'python']) {
      const result = spawnSync(python, [...PIP_ARGS, '-t', pythonDir], {
        cwd: LAYER_DIR,
        stdio: ['ignore', 2, 2],
      });
      if (result.status === 0) {
        stageShared(pythonDir);
        return true;
      }
    }
    return false;
  }
}

function sourceHash(): string {
  const hash = crypto.createHash('sha256');
  const files = [path.join(LAYER_DIR, 'requirements.txt'), CONFIG_FILE, IDENTITY_FILE, CEI_UI_SCHEMA_FILE,
    MODEL_POLICY_FILE, MODEL_DISCOVERY_FILE, HTTP_ERRORS_FILE];
  const walk = (dir: string): void => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== '__pycache__') walk(full);
      } else if (entry.name.endsWith('.py')) {
        files.push(full);
      }
    }
  };
  walk(PERSISTENCE_DIR);
  for (const file of files) {
    hash.update(path.relative(BACKEND_DIR, file)).update(fs.readFileSync(file));
  }
  return hash.digest('hex');
}

/**
 * Lambda layer with pinned pydantic deps plus the shared persistence code and http_errors.py.
 * Built during `cdk synth`/`cdk deploy` (CodeBuild, B91); the Docker image is only a fallback.
 */
export class PythonDepsLayer extends Construct {
  public readonly layer: lambda.LayerVersion;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // ADR: skip bundling under Jest | Rationale: unit tests must not run pip or Docker; the layer resource shape is what they assert
    // Alternative: CDK `aws:cdk:bundling-stacks` context rejected — every existing test file would have to set it
    const underJest = process.env.JEST_WORKER_ID !== undefined;

    const code = underJest
      ? lambda.Code.fromAsset(LAYER_DIR)
      : lambda.Code.fromAsset(LAYER_DIR, {
          // ADR: custom asset hash | Rationale: the asset dir holds only requirements.txt, so the default source hash would miss edits to the copied persistence code
          assetHashType: cdk.AssetHashType.CUSTOM,
          assetHash: sourceHash(),
          bundling: {
            image: lambda.Runtime.PYTHON_3_14.bundlingImage,
            local: new LocalPythonBundling(),
            volumes: [
              { hostPath: path.join(BACKEND_DIR, 'agents', 'shared'), containerPath: '/src/agents-shared' },
              { hostPath: path.join(BACKEND_DIR, 'lambdas', 'shared'), containerPath: '/src/lambdas-shared' },
            ],
            command: [
              'bash', '-c',
              [
                `pip install -r requirements.txt --platform manylinux2014_x86_64 --implementation cp --python-version 3.14 --only-binary=:all: -t /asset-output/python`,
                'mkdir -p /asset-output/python/shared && : > /asset-output/python/shared/__init__.py',
                'cp /src/agents-shared/config.py /asset-output/python/shared/config.py',
                'mkdir -p /asset-output/python/shared/runtime && : > /asset-output/python/shared/runtime/__init__.py && cp /src/agents-shared/runtime/identity.py /asset-output/python/shared/runtime/identity.py',
                'mkdir -p /asset-output/python/shared/schemas && : > /asset-output/python/shared/schemas/__init__.py && cp /src/agents-shared/schemas/cei_ui.py /asset-output/python/shared/schemas/cei_ui.py',
                'cp /src/agents-shared/runtime/model_policy.py /asset-output/python/shared/runtime/model_policy.py',
                'cp /src/agents-shared/runtime/model_discovery.py /asset-output/python/shared/runtime/model_discovery.py',
                'cp -r /src/agents-shared/persistence /asset-output/python/shared/persistence',
                'cp /src/lambdas-shared/http_errors.py /asset-output/python/http_errors.py',
                'find /asset-output/python -name __pycache__ -prune -exec rm -rf {} +',
              ].join(' && '),
            ],
          },
        });

    this.layer = new lambda.LayerVersion(this, 'Layer', {
      code,
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_14],
      compatibleArchitectures: [lambda.Architecture.X86_64],
      description: 'pydantic deps, shared persistence code, and http_errors helper for REST Lambdas',
    });
  }
}
