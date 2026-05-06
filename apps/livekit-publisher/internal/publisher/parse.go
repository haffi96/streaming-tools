package publisher

import (
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

var publishURLRegex = regexp.MustCompile(`^(h264|h265)://([^@/]+)@([^/]+?)(?:/(\d+)x(\d+))?$`)

func ParsePublishTarget(raw string) (PublishTarget, error) {
	matches := publishURLRegex.FindStringSubmatch(raw)
	if matches == nil {
		return PublishTarget{}, fmt.Errorf("invalid publish URL %q: expected <codec>://<name>@<host:port>[/<width>x<height>]", raw)
	}

	target := PublishTarget{
		Raw:     raw,
		Codec:   matches[1],
		Name:    matches[2],
		Address: matches[3],
		Network: "tcp",
	}

	if target.Name == "" {
		return PublishTarget{}, fmt.Errorf("publish URL %q is missing a track name", raw)
	}
	if target.Address == "" {
		return PublishTarget{}, fmt.Errorf("publish URL %q is missing a TCP address", raw)
	}
	if !strings.Contains(target.Address, ":") {
		return PublishTarget{}, fmt.Errorf("publish URL %q must use host:port TCP addressing", raw)
	}

	if matches[4] != "" || matches[5] != "" {
		width, err := strconv.ParseUint(matches[4], 10, 32)
		if err != nil || width == 0 {
			return PublishTarget{}, fmt.Errorf("publish URL %q has invalid width", raw)
		}
		height, err := strconv.ParseUint(matches[5], 10, 32)
		if err != nil || height == 0 {
			return PublishTarget{}, fmt.Errorf("publish URL %q has invalid height", raw)
		}
		target.Width = uint32(width)
		target.Height = uint32(height)
	}

	return target, nil
}

func GroupPublishTargets(targets []PublishTarget) ([]PublishGroup, error) {
	if len(targets) == 0 {
		return nil, nil
	}

	byName := make(map[string][]PublishTarget)
	for _, target := range targets {
		byName[target.Name] = append(byName[target.Name], target)
	}

	names := make([]string, 0, len(byName))
	for name := range byName {
		names = append(names, name)
	}
	slices.Sort(names)

	groups := make([]PublishGroup, 0, len(names))
	for _, name := range names {
		groupTargets := slices.Clone(byName[name])
		codec := groupTargets[0].Codec
		allHaveDims := true
		for _, target := range groupTargets {
			if target.Codec != codec {
				return nil, fmt.Errorf("track %q mixes codecs across publish URLs", name)
			}
			if !target.HasDimensions() {
				allHaveDims = false
			}
		}

		switch len(groupTargets) {
		case 1:
			groups = append(groups, PublishGroup{
				Kind:    PublishGroupIndependent,
				Name:    name,
				Codec:   codec,
				Targets: groupTargets,
			})
		default:
			if !allHaveDims {
				return nil, fmt.Errorf("track %q is duplicated but is not a valid simulcast group; repeated names require 2-3 URLs with dimensions", name)
			}
			if len(groupTargets) < 2 || len(groupTargets) > 3 {
				return nil, fmt.Errorf("track %q simulcast requires 2 or 3 layers, got %d", name, len(groupTargets))
			}
			for _, target := range groupTargets {
				if !target.HasDimensions() {
					return nil, fmt.Errorf("track %q simulcast layer %q is missing dimensions", name, target.Raw)
				}
			}
			slices.SortFunc(groupTargets, func(a, b PublishTarget) int {
				if a.Width != b.Width {
					if a.Width < b.Width {
						return -1
					}
					return 1
				}
				if a.Height < b.Height {
					return -1
				}
				if a.Height > b.Height {
					return 1
				}
				return strings.Compare(a.Address, b.Address)
			})
			groups = append(groups, PublishGroup{
				Kind:    PublishGroupSimulcast,
				Name:    name,
				Codec:   codec,
				Targets: groupTargets,
			})
		}
	}

	return groups, nil
}
