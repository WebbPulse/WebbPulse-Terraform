/** Contract shaped registry fixtures the registry tests build responses from. */

import type {
  Module,
  ModuleDocs,
  ModuleVersion,
  ModuleVersionDetail,
} from '../../api';

/** A published version, overridable field by field. */
export function aVersion(
  overrides: Partial<ModuleVersion> = {}
): ModuleVersion {
  return {
    version: '1.2.0',
    status: 'published',
    error: null,
    repository: 'WebbPulse/terraform-aws-network',
    tag: 'v1.2.0',
    sha: 'ded16beffd7111ef571296af462b637df8353202',
    actor: 'github',
    created_at: '2026-09-20T00:00:00Z',
    published_at: '2026-09-20T00:01:00Z',
    size_bytes: 2048,
    ...overrides,
  };
}

/** A connected module with a published and a failed version. */
export function aModule(overrides: Partial<Module> = {}): Module {
  return {
    namespace: 'WebbPulse',
    name: 'network',
    provider: 'aws',
    source: 'WebbPulse/network/aws',
    vcs_repo: 'WebbPulse/terraform-aws-network',
    created_at: '2026-09-19T00:00:00Z',
    versions: [
      aVersion({
        version: '1.3.0',
        tag: 'v1.3.0',
        status: 'failed',
        error: 'The tag has no .tf file at the repository root.',
        published_at: null,
        size_bytes: null,
      }),
      aVersion(),
    ],
    ...overrides,
  };
}

/** Documentation with one of everything, and a submodule. */
export function someDocs(overrides: Partial<ModuleDocs> = {}): ModuleDocs {
  return {
    readme: '# Network\n\nBuilds a **VPC**.\n\n<script>alert(1)</script>\n',
    inputs: [
      {
        name: 'cidr',
        type: 'string',
        description: 'The VPC range.',
        default: null,
        required: true,
        sensitive: false,
      },
      {
        name: 'tags',
        type: 'map(string)',
        description: 'Tags on every resource.',
        default: '{}',
        required: false,
        sensitive: false,
      },
    ],
    outputs: [{ name: 'vpc_id', description: 'The VPC id.', sensitive: false }],
    providers: [{ name: 'aws', source: 'hashicorp/aws', version: '>= 5.0' }],
    resources: [{ type: 'aws_vpc', name: 'this' }],
    submodules: [
      {
        name: 'subnets',
        path: 'modules/subnets',
        readme: null,
        inputs: [
          {
            name: 'vpc_id',
            type: 'string',
            description: null,
            default: null,
            required: true,
            sensitive: false,
          },
        ],
        outputs: [],
        providers: [],
        resources: [],
      },
    ],
    parse_errors: [],
    ...overrides,
  };
}

/** The detail of the published version with its documentation. */
export function aVersionDetail(
  overrides: Partial<ModuleVersionDetail> = {}
): ModuleVersionDetail {
  const module = aModule();
  return {
    namespace: module.namespace,
    name: module.name,
    provider: module.provider,
    source: module.source,
    vcs_repo: module.vcs_repo ?? null,
    created_at: module.created_at ?? null,
    version: aVersion(),
    docs: someDocs(),
    ...overrides,
  };
}
