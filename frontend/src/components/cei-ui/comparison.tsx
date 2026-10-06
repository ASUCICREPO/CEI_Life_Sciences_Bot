import type { ComparisonComponent } from '../../lib/cei-ui/schema';

export function ComparisonView({ component }: { component: ComparisonComponent }) {
  return (
    <figure className="my-3">
      {component.title && <figcaption className="text-sm font-semibold text-text-primary mb-1.5">{component.title}</figcaption>}
      <div className="overflow-x-auto rounded-lg border border-border-subtle">
        <table className="w-full text-xs">
          <thead className="bg-surface-panel">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-semibold text-text-primary">Criteria</th>
              {component.options.map((option, index) => <th key={`${option}-${index}`} scope="col" className="px-3 py-2 text-left font-semibold text-text-primary">{option}</th>)}
            </tr>
          </thead>
          <tbody>
            {component.criteria.map((criterion, criterionIndex) => (
              <tr key={`${criterion.label}-${criterionIndex}`} className="border-t border-border-subtle">
                <th scope="row" className="px-3 py-2 text-left font-medium text-text-primary">{criterion.label}</th>
                {criterion.values.map((value, optionIndex) => {
                  const isBest = criterion.best_index === optionIndex;
                  return (
                    <td key={optionIndex} className={`px-3 py-2 text-text-primary ${isBest ? 'bg-success-bg font-medium' : ''}`}>
                      {value}
                      {isBest && <span className="ml-1 text-success"> (best)</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {component.recommendation && <p className="mt-2 text-xs text-text-secondary italic">{component.recommendation}</p>}
    </figure>
  );
}
