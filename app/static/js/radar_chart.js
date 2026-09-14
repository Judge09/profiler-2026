/**
 * Hexagonal Radar / Stat Chart — Chart.js
 * Reads initial data from <script id="radarData" type="application/json">
 */

(function () {
  const dataEl = document.getElementById('radarData');
  if (!dataEl) return;

  let radarData;
  try {
    radarData = JSON.parse(dataEl.textContent);
  } catch (e) {
    return;
  }

  const ctx = document.getElementById('radarChart');
  if (!ctx) return;

  const ACCENT  = 'rgba(0, 229, 255, 1)';
  const FILL    = 'rgba(0, 229, 255, 0.15)';
  const GRID    = 'rgba(0, 229, 255, 0.08)';
  const TICK    = 'rgba(0, 229, 255, 0.25)';
  const LABEL   = '#8ba0c0';
  const POINT   = 'rgba(0, 229, 255, 1)';

  const chartConfig = {
    type: 'radar',
    data: {
      labels: radarData.labels,
      datasets: [{
        label: 'Profile',
        data: radarData.scores,
        fill: true,
        backgroundColor: FILL,
        borderColor: ACCENT,
        borderWidth: 2,
        pointBackgroundColor: POINT,
        pointBorderColor: '#0a0a14',
        pointBorderWidth: 2,
        pointRadius: 5,
        pointHoverRadius: 7,
        tension: 0.1,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: true,
      animation: {
        duration: 400,
        easing: 'easeInOutQuart',
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#0f0f1a',
          borderColor: 'rgba(0,229,255,0.3)',
          borderWidth: 1,
          titleColor: '#00e5ff',
          bodyColor: '#d0d8ef',
          padding: 10,
          callbacks: {
            label: (ctx) => ` ${ctx.raw.toFixed(1)} / 10`,
          }
        }
      },
      scales: {
        r: {
          min: 0,
          max: 10,
          ticks: {
            stepSize: 2,
            color: TICK,
            backdropColor: 'transparent',
            font: { size: 9, family: "'JetBrains Mono', monospace" },
            showLabelBackdrop: false,
          },
          grid: {
            color: GRID,
            lineWidth: 1,
          },
          angleLines: {
            color: GRID,
            lineWidth: 1,
          },
          pointLabels: {
            color: LABEL,
            font: {
              size: 11,
              family: "'Inter', sans-serif",
              weight: '600',
            },
          },
        }
      }
    }
  };

  const radarChart = new Chart(ctx, chartConfig);

  // Expose update function for the edit panel
  window.updateRadarChart = function (labels, scores) {
    radarChart.data.labels = labels;
    radarChart.data.datasets[0].data = scores;
    radarChart.update('active');
  };
})();
