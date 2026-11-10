import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { CompanionPresence } from '../components/companion/CompanionPresence';
import { CompanionEyes } from '../components/companion/CompanionEyes';
import { StateRecord, CompanionState } from '../companion/stateMachine';
import { OperatorSessionState } from '../types';

describe('P11.2.3 Companion Presence & Visual Eye Component Tests', () => {
  const baseRecord: StateRecord = {
    state: 'idle',
    source: 'supervisor_lifecycle',
    reason: 'Core online and verified',
    sequenceNumber: 2,
    timestamp: Date.now(),
  };

  const unpairedSession: OperatorSessionState = {
    paired: false,
  };

  const pairedSession: OperatorSessionState = {
    paired: true,
    approver: 'local_companion_operator',
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('CompanionEyes Component', () => {
    it('renders vector SVG with dual eye rings and irises in idle state', () => {
      const { container } = render(<CompanionEyes state="idle" size="md" />);

      const svg = container.querySelector('svg');
      expect(svg).toBeInTheDocument();
      expect(svg).toHaveAttribute('aria-hidden', 'true');

      const outerRings = container.querySelectorAll('.eye-outer-ring');
      expect(outerRings.length).toBe(2);

      const irises = container.querySelectorAll('.eye-iris');
      expect(irises.length).toBe(2);
    });

    it('renders closed eye paths in offline state', () => {
      const { container } = render(<CompanionEyes state="offline" size="md" />);

      const paths = container.querySelectorAll('path');
      expect(paths.length).toBe(2); // Closed slits for left and right eyes
      // Open circles should not exist when closed/sleeping
      expect(container.querySelectorAll('.eye-iris').length).toBe(0);
    });

    it('renders error state eye geometry with alert colors', () => {
      const { container } = render(<CompanionEyes state="error" size="lg" />);

      const wrapper = container.querySelector('.pico-companion-eyes');
      expect(wrapper).toHaveAttribute('data-state', 'error');
      expect(container.querySelectorAll('.eye-outer-ring').length).toBe(2);
    });

    it('pauses blink timers when document becomes hidden (minimize-to-tray lifecycle)', () => {
      const { container } = render(<CompanionEyes state="idle" size="md" />);

      act(() => {
        Object.defineProperty(document, 'visibilityState', {
          configurable: true,
          get: () => 'hidden',
        });
        document.dispatchEvent(new Event('visibilitychange'));
      });

      const wrapper = container.querySelector('.pico-companion-eyes');
      expect(wrapper).toHaveAttribute('data-blinking', 'false');

      // Restore visibility
      act(() => {
        Object.defineProperty(document, 'visibilityState', {
          configurable: true,
          get: () => 'visible',
        });
        document.dispatchEvent(new Event('visibilitychange'));
      });
    });
  });

  describe('CompanionPresence Header & Compact Views', () => {
    it('renders in header mode with state badge and live status region', () => {
      render(
        <CompanionPresence
          stateRecord={baseRecord}
          sessionState={pairedSession}
          isCompactMode={false}
          onToggleCompactMode={vi.fn()}
          onOpenPairingModal={vi.fn()}
          onRefreshHealth={vi.fn()}
        />
      );

      // Section has accessible label
      const section = screen.getByLabelText('PICO Companion Presence');
      expect(section).toHaveClass('header-mode');

      // Status badge is aria-live polite
      const statusBadge = screen.getByRole('status');
      expect(statusBadge).toHaveTextContent('Ready & Calm');
      expect(statusBadge).toHaveAttribute('aria-live', 'polite');

      // Operator pairing pill displays approver
      expect(screen.getByText('local_companion_operator')).toBeInTheDocument();
    });

    it('renders in compact mode with reason text, source tag, and large visual eyes', () => {
      render(
        <CompanionPresence
          stateRecord={baseRecord}
          sessionState={unpairedSession}
          isCompactMode={true}
          onToggleCompactMode={vi.fn()}
          onOpenPairingModal={vi.fn()}
          onRefreshHealth={vi.fn()}
        />
      );

      const section = screen.getByLabelText('PICO Companion Presence');
      expect(section).toHaveClass('compact-mode');

      // Reason text visible in compact mode
      expect(screen.getByText('Core online and verified')).toBeInTheDocument();
      expect(screen.getByText('Source: supervisor_lifecycle')).toBeInTheDocument();

      // Unpaired button present
      expect(screen.getByText('Pair Operator')).toBeInTheDocument();
    });

    it('dispatches onToggleCompactMode when toggle button is clicked', () => {
      const toggleSpy = vi.fn();
      render(
        <CompanionPresence
          stateRecord={baseRecord}
          sessionState={pairedSession}
          isCompactMode={false}
          onToggleCompactMode={toggleSpy}
          onOpenPairingModal={vi.fn()}
          onRefreshHealth={vi.fn()}
        />
      );

      const toggleBtn = screen.getByRole('button', { name: 'Switch to Compact Companion' });
      fireEvent.click(toggleBtn);
      expect(toggleSpy).toHaveBeenCalledTimes(1);
    });

    it('dispatches onRefreshHealth when refresh button is clicked', () => {
      const refreshSpy = vi.fn();
      render(
        <CompanionPresence
          stateRecord={baseRecord}
          sessionState={pairedSession}
          isCompactMode={false}
          onToggleCompactMode={vi.fn()}
          onOpenPairingModal={vi.fn()}
          onRefreshHealth={refreshSpy}
          isRefreshing={false}
        />
      );

      const refreshBtn = screen.getByRole('button', { name: 'Refresh core health' });
      fireEvent.click(refreshBtn);
      expect(refreshSpy).toHaveBeenCalledTimes(1);
    });

    it('dispatches onOpenPairingModal when pairing pill is clicked', () => {
      const pairSpy = vi.fn();
      render(
        <CompanionPresence
          stateRecord={baseRecord}
          sessionState={unpairedSession}
          isCompactMode={false}
          onToggleCompactMode={vi.fn()}
          onOpenPairingModal={pairSpy}
          onRefreshHealth={vi.fn()}
        />
      );

      const pairBtn = screen.getByRole('button', { name: 'Pair operator' });
      fireEvent.click(pairBtn);
      expect(pairSpy).toHaveBeenCalledTimes(1);
    });

    it('renders distinct state labels accurately across all defined companion states', () => {
      const states: CompanionState[] = [
        'idle',
        'listening',
        'thinking',
        'speaking',
        'executing',
        'awaiting_approval',
        'offline',
        'error',
      ];

      for (const st of states) {
        const record: StateRecord = {
          state: st,
          source: 'supervisor_lifecycle',
          reason: `Testing state ${st}`,
          sequenceNumber: 1,
          timestamp: Date.now(),
        };

        const { unmount } = render(
          <CompanionPresence
            stateRecord={record}
            sessionState={pairedSession}
            isCompactMode={false}
            onToggleCompactMode={vi.fn()}
            onOpenPairingModal={vi.fn()}
            onRefreshHealth={vi.fn()}
          />
        );

        const badge = screen.getByRole('status');
        expect(badge).toHaveAttribute('data-state', st);
        unmount();
      }
    });
  });
});
