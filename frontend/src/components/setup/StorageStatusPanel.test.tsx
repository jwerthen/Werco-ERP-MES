import React from 'react';
import { render, screen } from '@testing-library/react';
import { StorageStatusPanel } from './StorageStatusPanel';
import type { StorageStatus } from '../../types/setup';

const s3: StorageStatus = {
  backend: 's3',
  durability: 'durable',
  bucket: 'example-docs',
  endpoint_host: 'storage.example.test',
  region: 'us-east-1',
  upload_dir_configured: null,
  upload_dir_exists: null,
  upload_dir_on_mounted_volume: null,
  warning: null,
};

const local = (overrides: Partial<StorageStatus> = {}): StorageStatus => ({
  backend: 'local',
  durability: 'volume',
  bucket: null,
  endpoint_host: null,
  region: null,
  upload_dir_configured: true,
  upload_dir_exists: true,
  upload_dir_on_mounted_volume: true,
  warning: null,
  ...overrides,
});

describe('StorageStatusPanel', () => {
  it('does not guess a location the server withheld', () => {
    render(<StorageStatusPanel storage={{ ...s3, bucket: null, endpoint_host: null, region: null }} />);
    expect(screen.getByText('Location shown to Admins only')).toBeInTheDocument();
    expect(screen.queryByText(/AWS S3/)).not.toBeInTheDocument();
  });

  it('names the bucket and endpoint host for object storage', () => {
    render(<StorageStatusPanel storage={s3} />);
    expect(screen.getByText('Object storage')).toBeInTheDocument();
    expect(screen.getByText('example-docs @ storage.example.test')).toBeInTheDocument();
    expect(screen.getByText('durable')).toBeInTheDocument();
  });

  it('falls back to the AWS region when no custom endpoint is configured', () => {
    render(<StorageStatusPanel storage={{ ...s3, endpoint_host: null }} />);
    expect(screen.getByText('example-docs @ AWS S3 (us-east-1)')).toBeInTheDocument();
  });

  it('reports a mounted volume for local disk', () => {
    render(<StorageStatusPanel storage={local()} />);
    expect(screen.getByText('Local disk')).toBeInTheDocument();
    expect(screen.getByText('Mounted volume found')).toBeInTheDocument();
    expect(screen.getByText('Set explicitly, exists')).toBeInTheDocument();
  });

  it('shows the server warning verbatim for ephemeral local disk', () => {
    const warning = 'Files uploaded to this service are lost on the next redeploy.';
    render(
      <StorageStatusPanel
        storage={local({
          durability: 'ephemeral',
          upload_dir_on_mounted_volume: false,
          upload_dir_configured: false,
          warning,
        })}
      />
    );
    expect(screen.getByText('No mounted volume found')).toBeInTheDocument();
    expect(screen.getByText('Default location, exists')).toBeInTheDocument();
    expect(screen.getByText(warning)).toHaveClass('text-red-200');
  });

  it('renders an unknown mount state as amber', () => {
    render(
      <StorageStatusPanel
        storage={local({ durability: 'unknown', upload_dir_on_mounted_volume: null, warning: 'Confirm a volume.' })}
      />
    );
    expect(screen.getByText('Could not be read')).toBeInTheDocument();
    expect(screen.getByText('Confirm a volume.')).toHaveClass('text-amber-200');
  });

  it('distinguishes an unreadable configuration from an API that predates the field', () => {
    const { rerender } = render(<StorageStatusPanel storage={null} />);
    expect(screen.getByText('Storage configuration unavailable.')).toBeInTheDocument();
    rerender(<StorageStatusPanel storage={undefined} />);
    expect(screen.getByText(/does not report its storage configuration/)).toBeInTheDocument();
  });

  it('withdraws the summary when the last refresh failed', () => {
    render(<StorageStatusPanel storage={s3} stale />);
    expect(screen.getByText(/Current storage configuration is unknown/)).toBeInTheDocument();
    expect(screen.queryByText('Object storage')).not.toBeInTheDocument();
  });

  it('shows the region on its own row only beside a custom endpoint host', () => {
    const { rerender } = render(<StorageStatusPanel storage={s3} />);
    expect(screen.getByText('Region')).toBeInTheDocument();
    expect(screen.getByText('us-east-1')).toBeInTheDocument();
    rerender(<StorageStatusPanel storage={{ ...s3, endpoint_host: null }} />);
    expect(screen.queryByText('Region')).not.toBeInTheDocument();
  });

  it('shows the endpoint host alone when no bucket is reported', () => {
    render(<StorageStatusPanel storage={{ ...s3, bucket: null }} />);
    expect(screen.getByText('storage.example.test')).toBeInTheDocument();
    expect(screen.queryByText(/@/)).not.toBeInTheDocument();
  });

  it('says when the upload folder does not exist yet', () => {
    render(<StorageStatusPanel storage={local({ upload_dir_exists: false })} />);
    expect(screen.getByText('Set explicitly, does not exist yet')).toBeInTheDocument();
  });

  it('renders no warning notice when the server sends none', () => {
    const { container } = render(<StorageStatusPanel storage={local()} />);
    expect(container.querySelector('.text-red-200, .text-amber-200')).toBeNull();
  });

  it('is a labelled region that carries the durability badge and the restore caveat', () => {
    render(<StorageStatusPanel storage={local({ durability: 'ephemeral', warning: 'Lost on redeploy.' })} />);
    expect(screen.getByRole('region', { name: 'Document Storage' })).toBeInTheDocument();
    expect(screen.getByText('ephemeral')).toBeInTheDocument();
    expect(screen.getByText(/database restore does not bring uploaded files back/)).toBeInTheDocument();
  });
});
