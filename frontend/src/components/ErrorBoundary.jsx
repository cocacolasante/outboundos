import { Component } from 'react';

/**
 * Class-based error boundary. Catches render errors in the subtree and
 * shows a recoverable fallback UI instead of a blank page.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('ErrorBoundary caught:', error, info?.componentStack);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div data-testid="error-boundary" className="min-h-96 flex flex-col items-center justify-center gap-4 p-8">
          <div className="bg-red-50 border border-red-200 rounded-xl p-6 max-w-lg text-center">
            <h2 className="text-lg font-semibold text-red-800 m-0">Something went wrong</h2>
            <p className="text-sm text-red-600 font-mono mt-2">
              {this.state.error?.message || 'An unexpected error occurred.'}
            </p>
            <button
              type="button"
              onClick={this.handleReset}
              className="mt-4 inline-flex items-center px-4 py-2 bg-white hover:bg-slate-50 text-slate-700 text-sm font-medium border border-slate-300 rounded-lg transition-colors cursor-pointer"
            >
              Try again
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
