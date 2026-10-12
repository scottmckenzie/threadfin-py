package src

import (
	"encoding/json"
	"os"
	"sort"
	"strconv"
)

type filterCase struct {
	Filters map[string]map[string]interface{} `json:"filters"`
	Streams []map[string]string               `json:"streams"`
}

type filterResult struct {
	Active    bool `json:"active"`
	LiveEvent bool `json:"liveEvent"`
}

type filterOut struct {
	Error   bool           `json:"error"`
	Rules   []Filter       `json:"rules"`
	Results []filterResult `json:"results"`
}

// DumpFilters runs the real createFilterRules() and filterThisStream().
// Go iterates Settings.Filter in random order, so each filter goes through
// createFilterRules on its own and the rules are joined in numeric key order.
func DumpFilters(input string) (string, error) {
	content, err := os.ReadFile(input)
	if err != nil {
		return "", err
	}
	var c filterCase
	if err = json.Unmarshal(content, &c); err != nil {
		return "", err
	}
	keys := make([]int, 0, len(c.Filters))
	for k := range c.Filters {
		n, _ := strconv.Atoi(k)
		keys = append(keys, n)
	}
	sort.Ints(keys)

	out := filterOut{Rules: []Filter{}, Results: []filterResult{}}
	var all []Filter
	for _, n := range keys {
		Settings.Filter = map[int64]interface{}{0: c.Filters[strconv.Itoa(n)]}
		if err := createFilterRules(); err != nil {
			out.Error = true
			b, _ := json.MarshalIndent(out, "", "  ")
			return string(b), nil
		}
		all = append(all, Data.Filter...)
	}
	Data.Filter = all
	if all != nil {
		out.Rules = all
	}
	for _, s := range c.Streams {
		st, live := filterThisStream(s)
		out.Results = append(out.Results, filterResult{st, live})
	}
	b, err := json.MarshalIndent(out, "", "  ")
	return string(b), err
}
