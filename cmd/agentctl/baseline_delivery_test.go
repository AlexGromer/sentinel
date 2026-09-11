package main

// `baseline update` строит окружение мозгу из ЧЕТЫРЁХ переменных, и обе половины этого — дефекты.
//
// ЧТО ЗАМЕРЕНО. `cmdBaseline` собирает ровно `RUN_MODE`, `TARGET_URL`, `ARTIFACT_DIR`, `PLAN_FILE`.
// Следствий два, и они противоположны по знаку:
//
//  1. ЧЕГО ТАМ НЕТ — не доезжает. Подкоманда не принимает ни `--run-config`, ни `--observe`, а
//     `flag.ExitOnError` на неизвестном флаге убивает процесс кодом 2 — то есть передать их
//     физически нечем. При этом control-api для `mode=baseline` принимает настройки, ПРИМЕНЯЕТ
//     личные умолчания, НАЗЫВАЕТ применённое в ответе 202 полем `inherited_defaults` и пишет рядом с
//     артефактами `run.yaml` — файл, который по своему же заголовку есть «конфигурация, под которой
//     прогон РЕАЛЬНО шёл». Прогон под ней не шёл: бюджеты и блок `auth` доезжают до мозга ТОЛЬКО
//     этим файлом, а путь к нему в argv не попадает.
//
//  2. ЧЕГО ТАМ НЕТ — доезжает ЧУЖОЕ. `SENTINEL_OBSERVE` не дописывается, а префикс `SENTINEL_`
//     разрешён аллоулистом `filteredEnv`, поэтому УНАСЛЕДОВАННОЕ значение переживает и доходит до
//     мозга. На пути `run` это закрыто НАМЕРЕННО и закреплено тестом (`observe_flag_test.go`:
//     «унаследованное значение ОДНО не доезжает»), здесь — открыто. Человек, у которого в оболочке
//     экспортирован `SENTINEL_OBSERVE=record`, получает отказ прогона со ссылкой на режим, которого
//     в этом запросе не выбирал.
//
// ПОЧЕМУ ТЕСТ ВЫГЛЯДИТ ТАК. Утверждается окружение, которое мозг РЕАЛЬНО получил (харнесс
// `brainStub` пишет его в `<repo>/env.txt`), а не форма argv и не текст исходника: утверждение о
// тексте мутация проходит насквозь. Тот же приём, что у `observe_flag_test.go` и
// `heal_llm_delivery_test.go`.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func brainEnvAfterBaseline(t *testing.T, args []string) string {
	t.Helper()
	repo := t.TempDir()
	t.Setenv("BRAIN_PYTHON", brainStub(t, repo))
	plan := filepath.Join(repo, "plan.json")
	if err := os.WriteFile(plan, []byte(`{"steps":[]}`), 0o644); err != nil {
		t.Fatal(err)
	}
	full := append([]string{"update", "--plan", plan}, args...)
	if rc := cmdBaseline(repo, full); rc != 0 {
		t.Fatalf("cmdBaseline%v = %d, want 0", full, rc)
	}
	env, err := os.ReadFile(filepath.Join(repo, "env.txt"))
	if err != nil {
		t.Fatalf("the brain stub never ran: %v", err)
	}
	return string(env)
}

// Половина 1: выбор, сделанный человеком, обязан доезжать — иначе `run.yaml` рядом с артефактами
// описывает прогон, которого не было.
func TestBaselineCarriesTheRunConfigItWasGiven(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the brain stub is a /bin/sh script")
	}
	got := brainEnvAfterBaseline(t, []string{"--run-config", "/tmp/run.yaml"})
	if !strings.Contains(got, "RUN_CONFIG=/tmp/run.yaml") {
		t.Errorf("`baseline update` не передал мозгу путь к RunConfig: бюджеты и блок `auth` доезжают "+
			"ТОЛЬКО этим файлом, а control-api уже назвал их применёнными в ответе 202 и положил "+
			"`run.yaml` рядом с артефактами.\nenv:\n%s", got)
	}
}

// Половина 2: чужое НЕ имеет права доезжать. Это тот же контракт, что закреплён для пути `run`, и
// разный ответ на один вопрос в двух подкомандах — сам по себе дефект.
func TestBaselineDoesNotInheritAnObservationModeNobodyChose(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("the brain stub is a /bin/sh script")
	}
	t.Setenv("SENTINEL_OBSERVE", "record")
	got := brainEnvAfterBaseline(t, nil)
	if strings.Contains(got, "SENTINEL_OBSERVE=record") {
		t.Errorf("унаследованный режим наблюдения доехал до мозга на пути baseline: прогон откажется "+
			"со ссылкой на режим, которого человек в этом запросе не выбирал. На пути `run` это "+
			"закрыто намеренно и закреплено тестом — два разных ответа на один вопрос.\nenv:\n%s", got)
	}
	// И обратная сторона: явный выбор доезжает, иначе «починка» удовлетворялась бы запретом всегда.
	got = brainEnvAfterBaseline(t, []string{"--observe", "off"})
	if !strings.Contains(got, "SENTINEL_OBSERVE=off") {
		t.Errorf("явный --observe не доехал до мозга на пути baseline.\nenv:\n%s", got)
	}
}
