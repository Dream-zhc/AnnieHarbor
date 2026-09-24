import {useEffect,useRef,useState,type KeyboardEvent} from 'react';

export interface SelectOption {
  value: string;
  label: string;
  sub?: string;
}

interface CustomSelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  disabled?: boolean;
  ariaLabel?: string;
  placeholder?: string;
  className?: string;
  id?: string;
}

export function CustomSelect({
  value,
  onChange,
  options,
  disabled = false,
  ariaLabel,
  placeholder = '请选择',
  className = '',
  id,
}: CustomSelectProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  const selectedOption = options.find(opt => opt.value === value);

  // Close when clicking outside
  useEffect(() => {
    if (!open) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const handleKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  const handleTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      setOpen(true);
    }
  };

  const handleOptionClick = (optValue: string) => {
    onChange(optValue);
    setOpen(false);
  };

  return (
    <div
      ref={containerRef}
      className={`custom-select-wrap ${className} ${open ? 'is-open' : ''} ${disabled ? 'is-disabled' : ''}`}
      id={id}
    >
      {/* Hidden native select for screen reader & test runner accessibility */}
      <select
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        onChange={e => onChange(e.target.value)}
        tabIndex={-1}
        className="sr-only"
        aria-hidden="true"
      >
        {options.map(opt => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>

      {/* Styled Trigger Button */}
      <button
        type="button"
        className="custom-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen(prev => !prev)}
        onKeyDown={handleTriggerKeyDown}
      >
        <span className="custom-select-label">
          {selectedOption ? selectedOption.label : placeholder}
        </span>
        <svg
          className="custom-select-arrow"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>

      {/* Custom Dropdown Popover */}
      {open && (
        <div
          ref={listRef}
          className="custom-select-popover"
          role="listbox"
          aria-label={ariaLabel}
        >
          {options.map(opt => {
            const isSelected = opt.value === value;
            return (
              <div
                key={opt.value}
                role="option"
                aria-selected={isSelected}
                className={`custom-select-item ${isSelected ? 'is-selected' : ''}`}
                onClick={() => handleOptionClick(opt.value)}
              >
                <div className="custom-select-item-content">
                  <span className="custom-select-item-text">{opt.label}</span>
                  {opt.sub && <span className="custom-select-item-sub">{opt.sub}</span>}
                </div>
                {isSelected && (
                  <svg
                    className="custom-select-check"
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                  >
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
