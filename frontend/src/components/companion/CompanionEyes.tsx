import React, { useEffect, useState } from 'react';
import { CompanionState } from '../../companion/stateMachine';

export interface CompanionEyesProps {
  state: CompanionState;
  size?: 'sm' | 'md' | 'lg';
  className?: string;
}

export const CompanionEyes: React.FC<CompanionEyesProps> = ({
  state,
  size = 'md',
  className = '',
}) => {
  const [isBlinking, setIsBlinking] = useState(false);
  const [isWindowVisible, setIsWindowVisible] = useState(
    typeof document !== 'undefined' ? document.visibilityState === 'visible' : true
  );

  // Monitor document visibility to pause animation loops when minimized or hidden
  useEffect(() => {
    const handleVisibilityChange = () => {
      setIsWindowVisible(document.visibilityState === 'visible');
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, []);

  // Occasional natural blink cycle when in 'idle' state and window is visible
  useEffect(() => {
    if (state !== 'idle' || !isWindowVisible) {
      setIsBlinking(false);
      return;
    }

    let blinkTimeout: number;
    let nextBlinkTimeout: number;

    const scheduleBlink = () => {
      // Natural blink interval: random between 3.5s and 6.5s
      const delay = 3500 + Math.random() * 3000;
      nextBlinkTimeout = window.setTimeout(() => {
        setIsBlinking(true);
        blinkTimeout = window.setTimeout(() => {
          setIsBlinking(false);
          scheduleBlink();
        }, 150); // Blink duration 150ms
      }, delay);
    };

    scheduleBlink();

    return () => {
      clearTimeout(blinkTimeout);
      clearTimeout(nextBlinkTimeout);
    };
  }, [state, isWindowVisible]);

  // Dimensions based on size
  const dimensions = {
    sm: { width: 80, height: 32, eyeR: 12, pupilR: 6, spacing: 44 },
    md: { width: 140, height: 56, eyeR: 20, pupilR: 10, spacing: 76 },
    lg: { width: 200, height: 80, eyeR: 28, pupilR: 14, spacing: 108 },
  }[size];

  // State-specific palette
  const stateColors = {
    idle: {
      glow: 'rgba(99, 102, 241, 0.4)',
      iris: '#6366f1',
      pupil: '#ffffff',
      ring: 'rgba(99, 102, 241, 0.6)',
    },
    listening: {
      glow: 'rgba(6, 182, 212, 0.5)',
      iris: '#06b6d4',
      pupil: '#ffffff',
      ring: '#22d3ee',
    },
    thinking: {
      glow: 'rgba(168, 85, 247, 0.5)',
      iris: '#a855f7',
      pupil: '#ffffff',
      ring: '#c084fc',
    },
    speaking: {
      glow: 'rgba(59, 130, 246, 0.5)',
      iris: '#3b82f6',
      pupil: '#ffffff',
      ring: '#60a5fa',
    },
    executing: {
      glow: 'rgba(16, 185, 129, 0.5)',
      iris: '#10b981',
      pupil: '#ffffff',
      ring: '#34d399',
    },
    awaiting_approval: {
      glow: 'rgba(245, 158, 11, 0.6)',
      iris: '#f59e0b',
      pupil: '#ffffff',
      ring: '#fbbf24',
    },
    offline: {
      glow: 'rgba(100, 116, 139, 0.15)',
      iris: '#475569',
      pupil: '#64748b',
      ring: 'rgba(100, 116, 139, 0.3)',
    },
    error: {
      glow: 'rgba(239, 68, 68, 0.5)',
      iris: '#ef4444',
      pupil: '#ffffff',
      ring: '#f87171',
    },
  }[state];

  const centerY = dimensions.height / 2;
  const leftX = (dimensions.width - dimensions.spacing) / 2;
  const rightX = leftX + dimensions.spacing;

  // Eye open/close ratio
  const isClosed = state === 'offline' || isBlinking;

  return (
    <div
      className={`pico-companion-eyes pico-eyes-${state} pico-eyes-${size} ${className}`}
      data-state={state}
      data-blinking={isBlinking}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
      }}
    >
      <svg
        width={dimensions.width}
        height={dimensions.height}
        viewBox={`0 0 ${dimensions.width} ${dimensions.height}`}
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        className="companion-eyes-svg"
      >
        <defs>
          <filter id={`glow-${size}-${state}`} x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
        </defs>

        {isClosed ? (
          // Sleeping / Blinking closed eye slits
          <g stroke={stateColors.iris} strokeWidth="3.5" strokeLinecap="round">
            {/* Left Eye Slit */}
            <path
              d={`M ${leftX - dimensions.eyeR + 4} ${centerY} Q ${leftX} ${centerY + (state === 'offline' ? 3 : 0)} ${leftX + dimensions.eyeR - 4} ${centerY}`}
            />
            {/* Right Eye Slit */}
            <path
              d={`M ${rightX - dimensions.eyeR + 4} ${centerY} Q ${rightX} ${centerY + (state === 'offline' ? 3 : 0)} ${rightX + dimensions.eyeR - 4} ${centerY}`}
            />
          </g>
        ) : (
          // Open Expressive Eyes
          <>
            {/* Left Eye Shell & Glow */}
            <circle
              cx={leftX}
              cy={centerY}
              r={dimensions.eyeR}
              fill="rgba(17, 20, 32, 0.9)"
              stroke={stateColors.ring}
              strokeWidth="2"
              filter={`url(#glow-${size}-${state})`}
              className="eye-outer-ring"
            />
            {/* Left Iris */}
            <circle
              cx={leftX}
              cy={centerY}
              r={dimensions.pupilR}
              fill={stateColors.iris}
              className="eye-iris"
            />
            {/* Left Highlight */}
            <circle
              cx={leftX - dimensions.pupilR * 0.35}
              cy={centerY - dimensions.pupilR * 0.35}
              r={dimensions.pupilR * 0.35}
              fill={stateColors.pupil}
              className="eye-pupil-highlight"
            />

            {/* Right Eye Shell & Glow */}
            <circle
              cx={rightX}
              cy={centerY}
              r={dimensions.eyeR}
              fill="rgba(17, 20, 32, 0.9)"
              stroke={stateColors.ring}
              strokeWidth="2"
              filter={`url(#glow-${size}-${state})`}
              className="eye-outer-ring"
            />
            {/* Right Iris */}
            <circle
              cx={rightX}
              cy={centerY}
              r={dimensions.pupilR}
              fill={stateColors.iris}
              className="eye-iris"
            />
            {/* Right Highlight */}
            <circle
              cx={rightX - dimensions.pupilR * 0.35}
              cy={centerY - dimensions.pupilR * 0.35}
              r={dimensions.pupilR * 0.35}
              fill={stateColors.pupil}
              className="eye-pupil-highlight"
            />
          </>
        )}
      </svg>
    </div>
  );
};
