from copy import copy
from collections import defaultdict
from datetime import datetime, timedelta
import logging
import re
import os
import sys
import tempfile

import backtrader as bt

import pandas as pd

from bokeh.models.widgets import Panel, Tabs
from bokeh.layouts import gridplot

from bokeh.embed import file_html
from bokeh.resources import CDN
from bokeh.util.browser import view

from jinja2 import Environment, PackageLoader

from .schemes import Scheme, Blackly

from .utils import get_dataname, get_datanames, get_source_id, \
    get_plotobjs, get_smallest_dataname, filter_obj
from .figure import FigurePage, FigureType, Figure
from .clock import DataClockHandler
from .helper.label import obj2label
from .helper.bokeh import generate_stylesheet
from .tab import BacktraderPlottingTab
from .tabs import AnalyzerTab, MetadataTab, LogTab, SourceTab

_logger = logging.getLogger(__name__)


if 'ipykernel' in sys.modules:
    from IPython.core.display import display, HTML  # noqa
    from bokeh.io import output_notebook, show
    output_notebook()


class BacktraderPlotting(metaclass=bt.MetaParams):

    '''
    BacktraderPlotting is the main component

    It acts as a connection between backtrader and the plotting functionality.
    It acts of multiple strategies and creates a figurepage containing all
    figures to plot.

    The methods plot and show will be called from within backtrader.
    plot -> the given strategy will be mapped to a empty figurepage
    show -> all figurepages will be filled with data and plotted

    The live client also uses this class to generate all figures to plot.
    '''

    params = (
        # scheme object for styling plots
        ('scheme', Blackly()),
        # output filename when running backtest
        ('filename', None),
        # individual plot options
        ('plotconfig', None),
        # output mode for plotting: show, save, memory
        ('output_mode', 'show'),
        # custom tabs
        ('tabs', []),
        # should default tabs be used
        ('use_default_tabs', True),
        # default filter to apply on plots
        ('filter', None),
    )

    def __init__(self, **kwargs):
        if not isinstance(self.p.scheme, Scheme):
            raise Exception('Provided scheme has to be a subclass'
                            + ' of btplotting.schemes.scheme.Scheme')
        # set new scheme instance for app, so source scheme
        # remains untouched
        self.scheme = copy(self.p.scheme)
        # apply additional parameters to override / set scheme settings
        for pname, pvalue in kwargs.items():
            setattr(self.scheme, pname, pvalue)

        self._iplot = None
        self._figurepages = {}
        # set tabs
        self.tabs = copy(self.p.tabs)
        if self.p.use_default_tabs:
            self.tabs += [
                AnalyzerTab, MetadataTab, SourceTab, LogTab]
        if not isinstance(self.tabs, list):
            raise Exception(
                'Param tabs needs to be a list containing tabs to display')
        for tab in self.tabs:
            if not issubclass(tab, BacktraderPlottingTab):
                raise Exception(
                    'Tab needs to be a subclass of'
                    + ' btplotting.tab.BacktraderPlottingTab')

    def _reset(self):
        '''
        Resets the app
        '''
        self._figurepages = {}

    def _configure_plotting(self, figid=0):
        '''
        Applies config from plotconfig param to objects
        '''
        fp = self.get_figurepage(figid)
        objs = get_plotobjs(fp.strategy, include_non_plotable=True)

        i = 0
        for d in objs:
            if not isinstance(d, bt.Strategy):
                self._configure_plotobject(d, i)
                i += 1
            for s in objs[d]:
                self._configure_plotobject(s, i)
                i += 1

    def _configure_plotobject(self, obj, idx):
        '''
        Applies config to a single object
        '''

        # patch every object to contain plotorder and plotid
        if not hasattr(obj.plotinfo, 'plotid'):
            obj.plotinfo.plotid = f'{FigureType.get_type(obj).name}{idx}'
        if not hasattr(obj.plotinfo, 'plotorder'):
            obj.plotinfo.plotorder = 0

        if self.p.plotconfig is None:
            return

        def apply_config(obj, config):
            for k, v in config.items():
                setattr(obj.plotinfo, k, v)

        for k, config in self.p.plotconfig.items():
            ctype, target = k.split(':')
            if ctype == 'r':  # regex
                label = obj2label(obj)
                m = re.match(target, label)
                if m:
                    apply_config(obj, config)
            elif ctype[0] == '#':  # index
                target_type, target_idx = target.split('-')
                # check if instance type matches
                if not isinstance(obj, FigureType.get_obj[target_type]):
                    continue
                if int(target_idx) != idx:
                    continue
                apply_config(obj, config)
            elif ctype == 'id':  # plotid
                plotid = obj.plotinfo.plotid
                if plotid is None or plotid != target:
                    continue
                apply_config(obj, config)
            else:
                raise RuntimeError(
                    f'Unknown config type in plotting config: {k}')

    def _get_plotobjects(self, figid=0, filter=None):
        '''
        Returns a filtered dict of objects to be plotted
        '''
        fp = self.get_figurepage(figid)
        objs = get_plotobjs(fp.strategy, order_by_plotmaster=True)
        filtered = {}
        for o in objs:
            if filter_obj(o, filter):
                continue
            childs = []
            for c in objs[o]:
                if not filter_obj(c, filter):
                    childs.append(c)
            filtered[o] = childs
        return filtered

    def _blueprint_strategy(self, figid=0, filter=None):
        '''
        Fills a FigurePage with Figures of all objects to be plotted
        '''
        fp = self.get_figurepage(figid)
        scheme = self.scheme
        fp.reset()
        fp.analyzers += [
            a for _, a in fp.strategy.analyzers.getitems()]

        # get the objects to be plotted
        objects = self._get_plotobjects(figid, filter)

        # create figures
        figures = []
        for parent, childs in objects.items():
            figure = Figure(
                fp=fp,
                scheme=scheme,
                master=parent,
                childs=childs)
            figure.plot(parent)
            for c in childs:
                figure.plot(c)
            figure.apply()
            figures.append(figure)

        # link axis
        for i in range(1, len(figures)):
            figures[i].figure.x_range = figures[0].figure.x_range

        # add figures to figurepage
        fp.figures += figures

        # volume figures
        if self.scheme.volume and self.scheme.voloverlay is False:
            for f in figures:
                if not f.get_type() == FigureType.DATA:
                    continue
                figure = Figure(
                    fp=fp,
                    scheme=scheme,
                    master=f.master,
                    childs=[],
                    type=FigureType.VOL)
                figure.plot_volume(f.master)
                figure.apply()
                fp.figures.append(figure)

        # apply all figurepage related functionality after all figures
        # are set
        fp.apply()

    def _blueprint_optreturn(self, figid=0):
        '''
        Fills a FigurePage with all objects from optimization process
        '''
        fp = self.get_figurepage(figid)
        optreturn = fp.optreturn
        fp.reset()
        fp.analyzers += [
            a for _, a in optreturn.analyzers.getitems()]

    def _create_data_clock(self, figid=0):
        '''
        Creates the data clock for the given figurepage
        '''
        fp = self.get_figurepage(figid)
        strategy = fp.strategy

        # collect all objects to generate data for
        objs = defaultdict(list)
        for f in fp.figures:
            dataname = get_dataname(f.master)
            objs[dataname].append(f.master)
            for c in f.childs:
                dataname = get_dataname(c)
                objs[dataname].append(c)

        # use smallest data as clock
        smallest = get_smallest_dataname(strategy, objs.keys())

        # store data clock details in figurepage
        fp.data_clock = DataClockHandler(strategy, smallest)
        fp.data_clock_objs = objs

    def _output_stylesheet(self, template='basic.css.j2'):
        '''
        Renders and returns the stylesheet
        '''
        return generate_stylesheet(self.scheme, template)

    def _output_plotfile(self, model, figid=0, filename=None,
                         template='basic.html.j2'):
        '''
        Outputs the plot file
        '''
        if filename is None:
            tmpdir = tempfile.gettempdir()
            filename = os.path.join(tmpdir, f'bt_bokeh_plot_{figid}.html')

        now = datetime.now()
        env = Environment(loader=PackageLoader('btplotting', 'templates'))
        templ = env.get_template(template)
        templ.globals['now'] = now.strftime('%Y-%m-%d %H:%M:%S')

        html = file_html(model,
                         template=templ,
                         resources=CDN,
                         template_variables=dict(
                             stylesheet=self._output_stylesheet(),
                             show_headline=self.scheme.show_headline,
                             headline=self.scheme.headline),
                         _always_new=True)

        with open(filename, 'w') as f:
            f.write(html)

        return filename

    def create_figurepage(self, obj, figid=0, start=None, end=None,
                          filter=None, filldata=True):
        '''
        Creates new FigurePage for given obj.
        The obj can be either an instance of bt.Strategy or bt.OptReturn
        '''
        fp = FigurePage(obj, self.scheme)
        if figid in self._figurepages:
            raise Exception(f'FigurePage with figid "{figid}" already exists')
        self._figurepages[figid] = fp

        if isinstance(obj, bt.Strategy):
            self._configure_plotting(figid)
            self._blueprint_strategy(figid, filter)
            self._create_data_clock(figid)
            if filldata:
                df = self.get_data(figid, start=start, end=end)
                fp.set_cds_columns_from_df(df)
        elif isinstance(obj, bt.OptReturn):
            self._blueprint_optreturn(figid)
        else:
            raise Exception(
                f'Unsupported plot source object: {str(type(obj))}')
        return figid, fp

    def update_figurepage(self, figid=0, filter=None):
        '''
        Updates the figurepage with the given figid
        '''
        self._blueprint_strategy(figid, filter)

    def get_figurepage(self, figid=0):
        '''
        Returns the FigurePage with the given figid
        '''
        if figid not in self._figurepages:
            raise Exception(f'FigurePage with figid "{figid}" does not exist')
        return self._figurepages[figid]

    def generate_model(self, figid=0):
        '''
        Generates bokeh model used for the current figurepage
        '''
        fp = self.get_figurepage(figid)
        if fp.strategy is not None:
            panels = self.generate_model_panels()
        else:
            panels = []

        for t in self.tabs:
            tab = t(self, fp, None)
            if tab.is_useable():
                panels.append(tab.get_panel())

        # set all tabs (filter out None)
        model = Tabs(tabs=list(filter(None.__ne__, panels)))
        # attach the model to the underlying figure for
        # later reference (e.g. unit test)
        fp.model = model

        return model

    def generate_model_panels(self, figid=0):
        '''
        Generates bokeh panels used for figurepage
        '''
        fp = self.get_figurepage(figid)

        # sort figures
        data_sort = {False: 0}
        for i, d in enumerate(
                get_datanames(fp.strategy, filter=False),
                start=1):
            data_sort[d] = i
        sorted_figs = list(fp.figures)
        sorted_figs.sort(key=lambda x: (
            x.get_plotorder(),
            data_sort[get_dataname(x.master)],
            x.get_type().value))

        # fill tabs
        multiple_tabs = self.scheme.multiple_tabs
        tabs = defaultdict(list)
        for f in sorted_figs:
            tab = f.get_plottab()
            if tab:
                tabs[tab].append(f)
            elif not multiple_tabs:
                tabs['Plots'].append(f)
            else:
                figtype = f.get_type()
                if figtype == FigureType.DATA:
                    tabs['Datas'].append(f)
                elif figtype == FigureType.OBS:
                    tabs['Observers'].append(f)
                elif figtype == FigureType.IND:
                    tabs['Indicators'].append(f)
                else:
                    raise Exception(f'Unknown FigureType "{figtype}"')

        # create panels for tabs
        panels = []
        for tab in tabs:
            if len(tabs[tab]) == 0:
                continue
            # configure xaxis visibility
            if self.scheme.xaxis_pos == 'bottom':
                for i, x in enumerate(tabs[tab]):
                    x.figure.xaxis.visible = (
                        False if i < len(tabs[tab]) - 1
                        else True)
            # create gridplot for panel
            g = gridplot([[x.figure] for x in tabs[tab]],
                         toolbar_options={'logo': None},
                         toolbar_location=self.scheme.toolbar_location,
                         sizing_mode=self.scheme.plot_sizing_mode,
                         )
            # append created panel
            panels.append(Panel(title=tab, child=g))

        return panels

    def get_data(self, figid=0, start=None, end=None, back=None,
                 preserveidx=False, fillgaps=False):
        '''
        Returns data for given figurepage
        '''
        fp = self.get_figurepage(figid)
        data_clock = fp.data_clock
        objs = fp.data_clock_objs

        # create dataframe
        int_idx = data_clock.get_index_list(start, end, back, preserveidx)
        dt_idx = data_clock.get_dt_list(start, end, back)
        start_dt, end_dt = dt_idx[0], dt_idx[-1]
        df = pd.DataFrame(
            data={
                'index': pd.Series(int_idx, dtype='int64'),
                'datetime': pd.Series(dt_idx, dtype='datetime64[ns]')})

        # generate data for all figurepage objects
        for d in objs:
            for obj in objs[d]:
                df_data = data_clock.get_data(
                    obj=obj, start=start_dt, end=end_dt, fillgaps=fillgaps)
                df = df.join(df_data)

        df.set_index('index')
        return df

    def get_last_idx(self, figid=0):
        '''
        Returns the last index of figurepage data
        '''
        fp = self.get_figurepage(figid)
        return len(fp.data_clock) - 1

    def is_iplot(self):
        '''
        Returns iplot value
        '''
        return self._iplot

    def plot_optmodel(self, obj):
        '''
        Plots a optimization model
        '''
        self._reset()
        self.plot(obj)

        # we support only one strategy at a time so pass fixed zero index
        # if we ran optresults=False then we have a full strategy object
        # -> pass it to get full plot
        return self.generate_model(0)

    def plot(self, obj, figid=0, numfigs=1, iplot=True, start=None,
             end=None, use=None, filter=None, **kwargs):
        '''
        Plot either a strategy or an optimization result
        This method is called by backtrader
        '''
        if numfigs > 1:
            raise Exception('numfigs must be 1')
        if use is not None:
            raise Exception('Different backends by "use" not supported')

        # set filter from params if none provided
        if not filter:
            filter = self.p.filter

        # create figurepage for obj
        self.create_figurepage(
            obj,
            figid=figid,
            start=start,
            end=end,
            filter=filter)

        # returns all figurepages
        return self._figurepages

    def show(self):
        '''
        Display a figure
        This method is called by backtrader
        '''
        for figid in self._figurepages:
            model = self.generate_model(figid)

            if self.p.output_mode in ['show', 'save']:
                if self.is_iplot():
                    css = self._output_stylesheet()
                    display(HTML(css))
                    show(model)
                else:
                    filename = self._output_plotfile(
                        model, figid, self.p.filename)
                    if self.p.output_mode == 'show':
                        view(filename)
            elif self.p.output_mode == 'memory':
                pass
            else:
                raise RuntimeError(
                    'Invalid parameter "output_mode"'
                    + f' with value: {self.p.output_mode}')
