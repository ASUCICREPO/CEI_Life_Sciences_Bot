import type { ComparisonComponent } from '@/lib/cei-ui/schema';

export function ComparisonView({ component }: { component: ComparisonComponent }) {
  return (
    <figure className="cei-comparison-figure">
      {component.title && <figcaption className="cei-comparison-title">{component.title}</figcaption>}
      <div className="cei-table-scroll">
        <table className="cei-comparison">
          <thead>
            <tr>
              <th scope="col">Criteria</th>
              {component.options.map((option, index) => <th key={`${option}-${index}`} scope="col">{option}</th>)}
            </tr>
          </thead>
          <tbody>
            {component.criteria.map((criterion, criterionIndex) => (
              <tr key={`${criterion.label}-${criterionIndex}`}>
                <th scope="row">{criterion.label}</th>
                {criterion.values.map((value, optionIndex) => {
                  const isBest = criterion.best_index === optionIndex;
                  return (
                    <td key={optionIndex} data-best={isBest ? 'true' : undefined}>
                      {value}
                      {isBest && <span className="cei-comparison-best"> (best)</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {component.recommendation && <p className="cei-comparison-recommendation">{component.recommendation}</p>}
    </figure>
  );
}
