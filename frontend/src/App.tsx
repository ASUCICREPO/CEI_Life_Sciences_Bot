import { BrowserRouter, Navigate, Routes, Route } from 'react-router-dom';
import { AppProvider } from './context/AppContext';
import { ProductTourProvider } from './components/tour/ProductTour';
import { AppShell } from './components/shell/AppShell';
import { HomePage } from './pages/HomePage';
import { AskPage } from './pages/AskPage';
import { HistoryPage } from './pages/HistoryPage';
import { TaskLauncherPage } from './pages/TaskLauncherPage';
import { PipelinePage } from './pages/PipelinePage';
import { SettingsPage } from './pages/SettingsPage';
import { ToolsPage } from './pages/ToolsPage';
import { AuthProvider } from './lib/auth/AuthProvider';
import { RequireAuth } from './lib/auth/RequireAuth';
import { RequireOnboarding, RequireIncompleteProfile } from './lib/auth/RequireOnboarding';
import { CallbackPage } from './lib/auth/CallbackPage';
import { OnboardingPage } from './pages/OnboardingPage';

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppProvider>
          <ProductTourProvider>
            <Routes>
              <Route path="/auth/callback" element={<CallbackPage />} />
              <Route element={<RequireAuth />}>
                <Route
                  path="/onboarding"
                  element={<RequireIncompleteProfile><OnboardingPage /></RequireIncompleteProfile>}
                />
                <Route element={<RequireOnboarding><AppShell /></RequireOnboarding>}>
                  <Route path="/" element={<HomePage />} />
                  <Route path="/home" element={<Navigate to="/" replace />} />
                  <Route path="/ask" element={<AskPage />} />
                  <Route path="/tasks" element={<TaskLauncherPage />} />
                  <Route path="/history" element={<HistoryPage />} />
                  <Route path="/tools" element={<ToolsPage />} />
                  <Route path="/pipeline" element={<PipelinePage />} />
                  <Route path="/settings" element={<SettingsPage />} />
                </Route>
              </Route>
            </Routes>
          </ProductTourProvider>
        </AppProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}

export default App;
