import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AppLayout } from '../components/layout/AppLayout';
import { ConnectivityProvider } from '../context/ConnectivityContext';
import fs from 'node:fs';
import path from 'node:path';

const mockMinimize = vi.fn().mockResolvedValue(undefined);
const mockClose = vi.fn().mockResolvedValue(undefined);
const mockIsTauri = vi.fn().mockReturnValue(false);

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: () => mockIsTauri(),
}));

vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => ({
    minimize: mockMinimize,
    close: mockClose,
  }),
}));

interface TauriCapabilityConfig {
  identifier: string;
  description?: string;
  windows: string[];
  permissions: string[];
}

interface TauriConfig {
  productName?: string;
  version?: string;
  identifier?: string;
  build?: {
    frontendDist?: string;
    devUrl?: string;
    beforeDevCommand?: string;
    beforeBuildCommand?: string;
  };
  app?: {
    windows?: Array<{
      label?: string;
      title?: string;
      [key: string]: unknown;
    }>;
    security?: {
      csp?: string | null;
      devCsp?: string | null;
    };
  };
  bundle?: {
    active?: boolean;
    [key: string]: unknown;
  };
}

interface PackageJson {
  scripts?: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
}

function parseCsp(csp: string): Map<string, string[]> {
  const directives = new Map<string, string[]>();
  const parts = csp
    .split(';')
    .map((p) => p.trim())
    .filter(Boolean);
  for (const part of parts) {
    const tokens = part.split(/\s+/).filter(Boolean);
    if (tokens.length > 0) {
      const [name, ...sources] = tokens;
      directives.set(name, sources);
    }
  }
  return directives;
}

describe('Desktop Shell & Window Controls (Milestone P11.2.1)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockIsTauri.mockReturnValue(false);
  });

  const defaultProps = {
    currentTab: 'chat' as const,
    onTabChange: vi.fn(),
    conversations: [],
    activeConversation: null,
    onSelectConversation: vi.fn(),
    onNewConversation: vi.fn(),
    pendingApprovalsCount: 0,
    sessionState: { paired: false, approver: '' },
    onOpenPairingModal: vi.fn(),
    classification: 'INTERNAL' as const,
  };

  it('renders standard browser layout without desktop window controls and marks header drag region with deep mode', () => {
    mockIsTauri.mockReturnValue(false);

    render(
      <ConnectivityProvider>
        <AppLayout {...defaultProps}>
          <div>Content</div>
        </AppLayout>
      </ConnectivityProvider>
    );

    const header = screen.getByRole('banner');
    expect(header).toHaveAttribute('data-tauri-drag-region', 'deep');
    expect(screen.queryByRole('group', { name: /window controls/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^minimize$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /close to tray/i })).not.toBeInTheDocument();
  });

  it('renders desktop window controls and wires minimize and close actions when isTauri() is true', async () => {
    mockIsTauri.mockReturnValue(true);

    render(
      <ConnectivityProvider>
        <AppLayout {...defaultProps}>
          <div>Content</div>
        </AppLayout>
      </ConnectivityProvider>
    );

    const minimizeBtn = await screen.findByRole('button', { name: /^minimize$/i });
    const closeBtn = await screen.findByRole('button', { name: /close to tray/i });

    expect(minimizeBtn).toBeInTheDocument();
    expect(closeBtn).toBeInTheDocument();

    // Minimize action
    fireEvent.click(minimizeBtn);
    await waitFor(() => {
      expect(mockMinimize).toHaveBeenCalledTimes(1);
    });

    // Close (hide to tray) action
    fireEvent.click(closeBtn);
    await waitFor(() => {
      expect(mockClose).toHaveBeenCalledTimes(1);
    });
  });

  describe('Tauri Capabilities & Security Configuration Guardrails', () => {
    it('enforces minimal, non-default capabilities in capabilities/default.json', () => {
      const capabilityPath = path.resolve(__dirname, '../../src-tauri/capabilities/default.json');
      const content: TauriCapabilityConfig = JSON.parse(fs.readFileSync(capabilityPath, 'utf-8'));

      // Must be scoped strictly to the main window
      expect(content.windows).toEqual(['main']);

      // Must never reintroduce the broad core:default group
      expect(content.permissions).not.toContain('core:default');
      expect(content.permissions).not.toContain('core:window:default');

      // Must contain only the strict minimum permissions required by frontend JavaScript and header double-click maximize
      const allowedPermissions = [
        'core:window:allow-start-dragging',
        'core:window:allow-internal-toggle-maximize',
        'core:window:allow-minimize',
        'core:window:allow-close',
      ];
      expect(content.permissions).toEqual(allowedPermissions);

      // Verify no unauthorized or dangerous capabilities exist
      for (const perm of content.permissions) {
        expect(perm).toMatch(/^core:window:allow-(start-dragging|internal-toggle-maximize|minimize|close)$/);
      }
    });

    it('enforces strict Content Security Policy directives in tauri.conf.json', () => {
      const configPath = path.resolve(__dirname, '../../src-tauri/tauri.conf.json');
      const config: TauriConfig = JSON.parse(fs.readFileSync(configPath, 'utf-8'));

      const security = config.app?.security;
      expect(security).toBeDefined();

      const prodCsp = security?.csp;
      expect(prodCsp).toBeDefined();
      expect(typeof prodCsp).toBe('string');

      const devCsp = security?.devCsp;
      expect(devCsp).toBeDefined();
      expect(typeof devCsp).toBe('string');

      const prodDirectives = parseCsp(prodCsp as string);

      // default-src: strict 'self' only
      expect(prodDirectives.get('default-src')).toEqual(["'self'"]);

      // script-src: strict 'self' only (no external domains, no unsafe-eval, no unsafe-inline)
      const scriptSrc = prodDirectives.get('script-src');
      expect(scriptSrc).toEqual(["'self'"]);
      expect(scriptSrc).not.toContain("'unsafe-eval'");
      expect(scriptSrc).not.toContain("'unsafe-inline'");

      // style-src: allows 'self' and React dynamic inline styles
      expect(prodDirectives.get('style-src')).toEqual(["'self'", "'unsafe-inline'"]);

      // img-src: allows 'self', data:, asset:, and Tauri asset localhost
      expect(prodDirectives.get('img-src')).toEqual([
        "'self'",
        'data:',
        'asset:',
        'https://asset.localhost',
      ]);

      // font-src: allows 'self' and data:
      expect(prodDirectives.get('font-src')).toEqual(["'self'", 'data:']);

      // connect-src: allows 'self', ipc:, and Tauri IPC endpoint
      expect(prodDirectives.get('connect-src')).toEqual([
        "'self'",
        'ipc:',
        'http://ipc.localhost',
      ]);

      // Development allowances must remain strictly confined to devCsp
      expect(devCsp).toContain('localhost:1420');
      expect(devCsp).toContain('ws:');
      expect(prodCsp).not.toContain('localhost:1420');
      expect(prodCsp).not.toContain('127.0.0.1:1420');
      expect(prodCsp).not.toContain('ws:');

      // Packaging/bundling must remain inactive in P11.2.1
      expect(config.bundle?.active).toBe(false);
    });

    it('validates Vite strict-port dev configuration and script alignment', () => {
      const pkgPath = path.resolve(__dirname, '../../package.json');
      const pkg: PackageJson = JSON.parse(fs.readFileSync(pkgPath, 'utf-8'));

      const tauriConfigPath = path.resolve(__dirname, '../../src-tauri/tauri.conf.json');
      const tauriConfig: TauriConfig = JSON.parse(fs.readFileSync(tauriConfigPath, 'utf-8'));

      // Normal browser dev script must remain on default port with browser-development profile
      expect(pkg.scripts?.dev).toMatch(/^vite(\s+--mode browser-development)?$/);

      // dev:tauri script must strictly bind port 1420 on localhost and fail fast with --strictPort
      const devTauriScript = pkg.scripts?.['dev:tauri'];
      expect(devTauriScript).toBeDefined();
      expect(devTauriScript).toContain('--port 1420');
      expect(devTauriScript).toContain('--strictPort');
      expect(devTauriScript).toContain('--host localhost');

      // Tauri build configuration must agree on devUrl and beforeDevCommand
      expect(tauriConfig.build?.devUrl).toBe('http://localhost:1420');
      expect(tauriConfig.build?.beforeDevCommand).toBe('pnpm run dev:tauri');
      expect(tauriConfig.build?.frontendDist).toBe('../dist');
      expect(tauriConfig.build?.beforeBuildCommand).toBe('pnpm run build');
    });
  });
});
