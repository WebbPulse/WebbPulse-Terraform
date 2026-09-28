import { describe, expect, it } from 'vitest';

import { aModule, aVersion } from './fixtures';
import {
  addressFromRepository,
  blockLabel,
  latestPublished,
  sourceAddress,
  tokenVariable,
  usageSnippet,
} from './moduleText';

describe('registry text', () => {
  it('names the source on the SPA host, with a submodule suffix', () => {
    const module = aModule();
    expect(sourceAddress('staging.terraform.webbpulse.com', module)).toBe(
      'staging.terraform.webbpulse.com/WebbPulse/network/aws'
    );
    expect(sourceAddress('example.com', module, 'subnets')).toBe(
      'example.com/WebbPulse/network/aws//modules/subnets'
    );
  });

  it('writes a module block with the version and each required input', () => {
    expect(
      usageSnippet('example.com/WebbPulse/network/aws', 'network', '1.2.0', [
        'cidr',
        'region',
      ])
    ).toBe(
      [
        'module "network" {',
        '  source  = "example.com/WebbPulse/network/aws"',
        '  version = "1.2.0"',
        '',
        '  cidr   = # required',
        '  region = # required',
        '}',
      ].join('\n')
    );
    expect(usageSnippet('h/a/b/c', 'b', null)).not.toContain('version');
  });

  it('makes a valid block label from any name', () => {
    expect(blockLabel('my.module')).toBe('my_module');
    expect(blockLabel('1st')).toBe('m_1st');
  });

  it('opens on the newest published version', () => {
    const failed = aVersion({ version: '2.0.0', status: 'failed' });
    expect(latestPublished([failed, aVersion()])?.version).toBe('1.2.0');
    expect(latestPublished([failed])).toBeNull();
  });

  it('names the token variable as Terraform reads it', () => {
    expect(tokenVariable('staging.terraform.webbpulse.com')).toBe(
      'TF_TOKEN_staging_terraform_webbpulse_com'
    );
    expect(tokenVariable('my-host.example.com:5173')).toBe(
      'TF_TOKEN_my__host_example_com'
    );
  });

  it('derives the address a terraform-provider-name repository implies', () => {
    expect(addressFromRepository('WebbPulse/terraform-aws-network')).toEqual({
      provider: 'aws',
      name: 'network',
    });
    expect(addressFromRepository('WebbPulse/infra')).toBeNull();
  });
});
